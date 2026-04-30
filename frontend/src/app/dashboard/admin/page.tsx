'use client';

import { Fragment, useCallback, useEffect, useId, useState } from 'react';
import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';
import { useAuth } from '@/components/providers';
import {
  AdminUser,
  AdminRecentRequestItem,
  AdminRequestMetricsWindow,
  AuditLogEntry,
  BroadcastDetailResponse,
  BroadcastListItem,
  StatusCounts,
  UserDetail,
  UserSortBy,
  listUsers,
  getUserDetail,
  updateUser,
  approveUser,
  rejectUser,
  deleteUser,
  regenerateApiKeyAdmin,
  listAuditLog,
  listRecentRequests,
  getRequestMetrics,
  previewBroadcast,
  sendTestBroadcastEmail,
  createBroadcast,
  listBroadcasts,
  getBroadcastDetail,
  cancelBroadcast,
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

function previewText(value: string, maxChars: number = 280): string {
  if (value.length <= maxChars) return value;
  return `${value.slice(0, maxChars)}...`;
}

function applyOffsetJump(
  rawPage: string,
  total: number,
  pageSize: number,
  setOffset: (offset: number) => void,
  clearInput: () => void,
): void {
  const totalPages = Math.ceil(total / pageSize);
  const n = Number.parseInt(rawPage.trim(), 10);
  if (!Number.isFinite(n)) return;
  const p = Math.min(Math.max(1, n), totalPages);
  setOffset((p - 1) * pageSize);
  clearInput();
}

function FoldedText({ label, value }: { label: string; value?: string | null }) {
  if (!value) {
    return (
      <div className="col-span-full">
        <span className="text-gray-500">{label}:</span> <span className="text-gray-700">—</span>
      </div>
    );
  }

  return (
    <details className="col-span-full group">
      <summary className="cursor-pointer list-none text-gray-500 flex items-center gap-2">
        <span>{label}:</span>
        <span className="text-gray-700 whitespace-pre-wrap break-words">{previewText(value)}</span>
        <span className="text-[10px] text-gray-400 group-open:hidden">(show more)</span>
        <span className="text-[10px] text-gray-400 hidden group-open:inline">(show less)</span>
      </summary>
      <pre className="mt-1 overflow-x-auto rounded-md border border-gray-200 bg-white px-3 py-2 text-[11px] text-gray-700 whitespace-pre-wrap break-words">
        {value}
      </pre>
    </details>
  );
}

function formatLatency(ms?: number | null): string {
  if (ms == null) return '—';
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms)}ms`;
}

function RequestMetricsCard({ metric }: { metric: AdminRequestMetricsWindow }) {
  const maxRequests = Math.max(...metric.buckets.map((bucket) => bucket.request_count), 1);

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-[12px] font-medium text-gray-500">{metric.label}</div>
          <div className="mt-1 text-[24px] font-bold tabular-nums text-gray-900">
            {metric.total_requests.toLocaleString()}
          </div>
          <div className="text-[11px] text-gray-400">requests</div>
        </div>
        <div className="text-right text-[11px] text-gray-400">
          <div>
            <span className="text-emerald-600">{metric.success_requests.toLocaleString()}</span> ok
          </div>
          <div>
            <span className="text-red-500">{metric.error_requests.toLocaleString()}</span> err
          </div>
          <div>{formatLatency(metric.avg_latency_ms)} avg</div>
        </div>
      </div>
      <div className="mt-4 flex h-16 items-end gap-px overflow-hidden rounded-md bg-gray-50 px-1 py-1">
        {metric.buckets.map((bucket) => {
          const height =
            bucket.request_count === 0 ? 2 : (bucket.request_count / maxRequests) * 100;
          const isErrorHeavy = bucket.error_count > 0 && bucket.error_count >= bucket.success_count;
          return (
            <div
              key={bucket.start_time}
              className={`min-w-0 flex-1 rounded-t-sm ${
                isErrorHeavy ? 'bg-red-400' : 'bg-gray-900'
              }`}
              style={{ height: `${height}%` }}
              title={`${new Date(bucket.start_time).toLocaleString()}: ${
                bucket.request_count
              } requests, ${bucket.error_count} errors`}
            />
          );
        })}
      </div>
    </div>
  );
}

const AUDIT_ACTIONS = [
  'create_user',
  'approve_user',
  'reject_user',
  'update_user',
  'delete_user',
  'create_key',
  'revoke_key',
  'delete_key',
  'hard_delete_key',
  'regenerate_key',
  'update_key',
];

export default function AdminPage() {
  const { state } = useAuth();

  // Top-level tab
  const [activeTab, setActiveTab] = useState<'users' | 'audit' | 'requests' | 'broadcast'>('users');

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const tab = params.get('tab');
    if (tab === 'users' || tab === 'audit' || tab === 'requests' || tab === 'broadcast') {
      setActiveTab(tab as 'users' | 'audit' | 'requests' | 'broadcast');
    }
  }, []);

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
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [rejectTarget, setRejectTarget] = useState<AdminUser | null>(null);
  const [rejectReason, setRejectReason] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<AdminUser | null>(null);
  const [deleteReason, setDeleteReason] = useState('');
  const [newKey, setNewKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<UserDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [editRole, setEditRole] = useState('');
  const [editTier, setEditTier] = useState('');
  const [editQuota, setEditQuota] = useState('');
  const [saving, setSaving] = useState(false);

  // Broadcast email state
  const [broadcasts, setBroadcasts] = useState<BroadcastListItem[]>([]);
  const [broadcastTotal, setBroadcastTotal] = useState(0);
  const [broadcastLoading, setBroadcastLoading] = useState(false);
  const [broadcastDetail, setBroadcastDetail] = useState<BroadcastDetailResponse | null>(null);
  const [broadcastDetailLoading, setBroadcastDetailLoading] = useState(false);
  const [bcTemplateKey, setBcTemplateKey] = useState<string>('custom');
  const [bcTemplateVars, setBcTemplateVars] = useState<Record<string, string>>({});
  const [bcSubject, setBcSubject] = useState('');
  const [bcBodyHtml, setBcBodyHtml] = useState('');
  const [bcScheduleMode, setBcScheduleMode] = useState<'now' | 'later'>('now');
  const [bcScheduledAt, setBcScheduledAt] = useState('');
  const [bcPreview, setBcPreview] = useState<{ recipient_count: number; rendered_subject: string; rendered_body_html: string } | null>(null);
  const [bcPreviewLoading, setBcPreviewLoading] = useState(false);
  const [bcSending, setBcSending] = useState(false);
  const [bcConfirm, setBcConfirm] = useState(false);
  const [bcTestLoading, setBcTestLoading] = useState(false);
  const [bcTargetRoles, setBcTargetRoles] = useState<string[]>(['free', 'internal', 'admin']);
  const [bcTargetStatuses, setBcTargetStatuses] = useState<string[]>(['active']);

  // Audit log state
  const [auditEntries, setAuditEntries] = useState<AuditLogEntry[]>([]);
  const [auditTotal, setAuditTotal] = useState(0);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditFilter, setAuditFilter] = useState('');
  const [auditOffset, setAuditOffset] = useState(0);
  const AUDIT_PAGE_SIZE = 50;

  // Requests state
  const [reqEntries, setReqEntries] = useState<AdminRecentRequestItem[]>([]);
  const [reqTotal, setReqTotal] = useState(0);
  const [reqLoading, setReqLoading] = useState(false);
  const [reqOffset, setReqOffset] = useState(0);
  const [reqUserFilter, setReqUserFilter] = useState('');
  const [reqModelFilter, setReqModelFilter] = useState('');
  const [reqErrorsOnly, setReqErrorsOnly] = useState(false);
  const [reqExpandedId, setReqExpandedId] = useState<string | null>(null);
  const [reqJumpPage, setReqJumpPage] = useState('');
  const [reqMetrics, setReqMetrics] = useState<AdminRequestMetricsWindow[]>([]);
  const [reqMetricsLoading, setReqMetricsLoading] = useState(false);
  const reqJumpInputId = useId();
  const REQ_PAGE_SIZE = 50;

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const d = await listUsers(
        filter || undefined,
        100,
        0,
        searchTerm || undefined,
        sortBy !== 'created' ? sortBy : undefined,
      );
      setUsers(d.users);
      setCounts(d.status_counts);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [filter, searchTerm, sortBy]);

  const loadAudit = useCallback(async () => {
    setAuditLoading(true);
    setError(null);
    try {
      const d = await listAuditLog(
        auditFilter || undefined,
        undefined,
        AUDIT_PAGE_SIZE,
        auditOffset,
      );
      setAuditEntries(d.entries);
      setAuditTotal(d.total);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setAuditLoading(false);
    }
  }, [auditFilter, auditOffset]);

  const loadRequests = useCallback(async () => {
    setReqLoading(true);
    setError(null);
    try {
      const d = await listRecentRequests(
        REQ_PAGE_SIZE,
        reqOffset,
        reqUserFilter || undefined,
        reqModelFilter || undefined,
        reqErrorsOnly,
      );
      setReqEntries(d.requests);
      setReqTotal(d.total);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setReqLoading(false);
    }
  }, [reqOffset, reqUserFilter, reqModelFilter, reqErrorsOnly]);

  const loadRequestMetrics = useCallback(async () => {
    setReqMetricsLoading(true);
    setError(null);
    try {
      const d = await getRequestMetrics();
      setReqMetrics(d.windows);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setReqMetricsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (activeTab === 'users') load();
  }, [load, activeTab]);

  useEffect(() => {
    if (activeTab === 'audit') loadAudit();
  }, [loadAudit, activeTab]);

  useEffect(() => {
    if (activeTab === 'requests') {
      loadRequests();
      loadRequestMetrics();
    }
  }, [loadRequests, loadRequestMetrics, activeTab]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 4000);
    return () => clearTimeout(t);
  }, [toast]);

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
      setEditTier(d.key_tier || 'free');
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
      if (editTier !== (detail.key_tier || 'free')) u.tier = editTier;
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

  if (!state.user?.is_admin) {
    return (
      <ProtectedRoute>
        <div className="flex min-h-[50vh] flex-col items-center justify-center text-center">
          <h1 className="text-lg font-semibold text-gray-900">Admin access required</h1>
          <p className="mt-1 text-sm text-gray-500">
            You don&apos;t have permission to view this page.
          </p>
          <a
            href="/dashboard"
            className="mt-5 text-sm font-medium text-gray-900 underline decoration-gray-300 underline-offset-4 hover:decoration-gray-900 transition"
          >
            Back to Dashboard
          </a>
        </div>
      </ProtectedRoute>
    );
  }

  const filters = [
    { key: '', label: 'All', count: counts.all },
    { key: 'pending_approval', label: 'Pending', count: counts.pending_approval },
    { key: 'active', label: 'Active', count: counts.active },
    { key: 'rejected', label: 'Rejected', count: counts.rejected },
    { key: 'suspended', label: 'Suspended', count: counts.suspended },
    { key: 'deleted', label: 'Deleted', count: counts.deleted },
  ];

  const loadBroadcasts = useCallback(async () => {
    setBroadcastLoading(true);
    try {
      const res = await listBroadcasts(50, 0);
      setBroadcasts(res.broadcasts);
      setBroadcastTotal(res.total);
    } catch {
      // non-fatal
    } finally {
      setBroadcastLoading(false);
    }
  }, []);

  useEffect(() => {
    if (activeTab === 'broadcast') loadBroadcasts();
  }, [loadBroadcasts, activeTab]);

  const onTabChange = (tab: 'users' | 'audit' | 'requests' | 'broadcast') => {
    setActiveTab(tab);
    const params = new URLSearchParams(window.location.search);
    params.set('tab', tab);
    const next = `${window.location.pathname}?${params.toString()}`;
    window.history.replaceState({}, '', next);
  };

  const refreshActiveTab = () => {
    if (activeTab === 'users') { load(); return; }
    if (activeTab === 'audit') { loadAudit(); return; }
    if (activeTab === 'broadcast') { loadBroadcasts(); return; }
    loadRequests();
    loadRequestMetrics();
  };

  return (
    <ProtectedRoute>
      <div className="mx-auto w-full max-w-4xl pb-20">
        {/* Nav */}
        <div className="mb-10 flex items-center justify-between">
          <a
            href="/dashboard"
            className="group flex items-center gap-1.5 text-[13px] text-gray-400 transition hover:text-gray-900"
          >
            <svg
              className="h-3.5 w-3.5 transition group-hover:-translate-x-px"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2.5}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M10.5 19.5L3 12m0 0l7.5-7.5M3 12h18"
              />
            </svg>
            Dashboard
          </a>
          <button
            onClick={refreshActiveTab}
            disabled={loading || auditLoading || reqLoading || reqMetricsLoading}
            className="text-[13px] text-gray-400 transition hover:text-gray-900 disabled:opacity-40"
          >
            {loading || auditLoading || reqLoading || reqMetricsLoading ? 'Loading...' : 'Refresh'}
          </button>
        </div>

        {/* Title */}
        <h1 className="text-[28px] font-bold tracking-tight text-gray-900">Admin</h1>
        <p className="mt-0.5 text-[15px] text-gray-500">
          Manage users, API keys, quotas, and audit log.
        </p>

        {/* Top-level tab toggle */}
        <div className="mt-6 flex items-center gap-1">
          {(['users', 'requests', 'audit', 'broadcast'] as const).map((tab) => (
            <button
              key={tab}
              onClick={() => onTabChange(tab)}
              className={`rounded-md px-3.5 py-1.5 text-[13px] font-medium transition ${
                activeTab === tab
                  ? 'bg-gray-900 text-white'
                  : 'text-gray-500 hover:bg-gray-100 hover:text-gray-900'
              }`}
            >
              {tab === 'users' ? 'Users' : tab === 'requests' ? 'Recent Requests' : tab === 'audit' ? 'Audit Log' : 'Broadcast Email'}
            </button>
          ))}
        </div>

        {/* Alerts */}
        {error && (
          <div className="mt-4 rounded-lg bg-red-50 px-4 py-2.5 text-[13px] text-red-600">
            {error}{' '}
            <button onClick={() => setError(null)} className="ml-2 font-bold">
              &times;
            </button>
          </div>
        )}
        {toast && (
          <div className="mt-4 rounded-lg bg-gray-900 px-4 py-2.5 text-[13px] text-white">
            {toast}
          </div>
        )}

        {/* ========== Users Tab ========== */}
        {activeTab === 'users' && (
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
                  <span className="text-[13px] font-semibold text-gray-900">
                    New API key generated
                  </span>
                  <button
                    onClick={() => setNewKey(null)}
                    className="text-gray-300 hover:text-gray-500"
                  >
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
                              {u.has_key && u.key_tier && u.key_tier !== 'free' && (
                                <span className="font-semibold uppercase text-[10px] text-blue-600">
                                  {u.key_tier}
                                </span>
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
                              <div className="space-y-4 rounded-xl border border-gray-200 bg-gray-50 p-5">
                                {/* Stats */}
                                <div className="grid grid-cols-4 gap-3 text-[13px]">
                                  <div>
                                    <div className="text-[11px] font-medium text-gray-500">
                                      Today
                                    </div>
                                    <div className="mt-0.5 text-[16px] font-bold tabular-nums text-gray-900">
                                      ${Number(detail.usage_today_usd).toFixed(2)}
                                    </div>
                                    <div className="text-[11px] text-gray-400 tabular-nums">
                                      {detail.usage_today_requests} req
                                    </div>
                                  </div>
                                  <div>
                                    <div className="text-[11px] font-medium text-gray-500">
                                      This month
                                    </div>
                                    <div className="mt-0.5 text-[16px] font-bold tabular-nums text-gray-900">
                                      ${Number(detail.usage_month_usd).toFixed(2)}
                                    </div>
                                    <div className="text-[11px] text-gray-400 tabular-nums">
                                      {detail.usage_month_requests} req
                                    </div>
                                  </div>
                                  <div>
                                    <div className="text-[11px] font-medium text-gray-500">
                                      Last active
                                    </div>
                                    <div className="mt-0.5 text-[16px] font-bold text-gray-900">
                                      {relTime(detail.last_request_at)}
                                    </div>
                                  </div>
                                  <div>
                                    <div className="text-[11px] font-medium text-gray-500">
                                      Quota
                                    </div>
                                    <div className="mt-0.5 text-[16px] font-bold text-gray-900">
                                      {detail.quota_daily_usd
                                        ? `$${detail.quota_daily_usd}/d`
                                        : '-'}
                                    </div>
                                  </div>
                                </div>

                                {/* Models */}
                                {detail.models_used.length > 0 && (
                                  <div className="flex flex-wrap gap-1.5">
                                    {detail.models_used.map((m) => (
                                      <span
                                        key={m}
                                        className="rounded bg-white px-2 py-0.5 text-[11px] font-medium text-gray-600 border border-gray-200 shadow-sm"
                                      >
                                        {m.split('/').pop()}
                                      </span>
                                    ))}
                                  </div>
                                )}

                                {/* Edit (active users) */}
                                {u.status === 'active' && (
                                  <div className="flex items-end gap-3 border-t border-gray-200 pt-4">
                                    <div>
                                      <div className="text-[11px] font-medium text-gray-500 mb-1">
                                        Role
                                      </div>
                                      <select
                                        value={editRole}
                                        onChange={(e) => setEditRole(e.target.value)}
                                        className="rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-[13px]"
                                      >
                                        <option value="free">free</option>
                                        <option value="internal">internal</option>
                                        <option value="admin">admin</option>
                                      </select>
                                    </div>
                                    {detail.has_key && (
                                      <>
                                        <div>
                                          <div className="text-[11px] font-medium text-gray-500 mb-1">
                                            Tier
                                          </div>
                                          <select
                                            value={editTier}
                                            onChange={(e) => setEditTier(e.target.value)}
                                            className="rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-[13px]"
                                          >
                                            <option value="free">free</option>
                                            <option value="pro">pro</option>
                                            <option value="enterprise">enterprise</option>
                                          </select>
                                        </div>
                                        <div>
                                          <div className="text-[11px] font-medium text-gray-500 mb-1">
                                            Daily quota
                                          </div>
                                          <input
                                            type="number"
                                            value={editQuota}
                                            onChange={(e) => setEditQuota(e.target.value)}
                                            className="w-24 rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-[13px]"
                                          />
                                        </div>
                                      </>
                                    )}
                                    <button
                                      onClick={doSave}
                                      disabled={saving}
                                      className="rounded-md bg-gray-900 px-3 py-1.5 text-[12px] font-semibold text-white hover:bg-gray-800 transition disabled:opacity-50"
                                    >
                                      {saving ? '...' : 'Save'}
                                    </button>
                                    <div className="flex-1" />
                                    <button
                                      onClick={() => {
                                        setDeleteTarget(u);
                                        setDeleteReason('');
                                      }}
                                      className="text-[12px] text-red-400 hover:text-red-600 transition"
                                    >
                                      Delete
                                    </button>
                                    <button
                                      onClick={() => doSuspend(u.id)}
                                      className="text-[12px] text-red-400 hover:text-red-600 transition"
                                    >
                                      Suspend
                                    </button>
                                  </div>
                                )}

                                {/* Suspended users */}
                                {u.status === 'suspended' && (
                                  <div className="flex items-center justify-between border-t border-gray-200 pt-4">
                                    <span className="text-[13px] text-gray-500">
                                      This user is suspended.
                                    </span>
                                    <div className="flex items-center gap-3">
                                      <button
                                        onClick={() => {
                                          setDeleteTarget(u);
                                          setDeleteReason('');
                                        }}
                                        className="text-[12px] text-red-400 hover:text-red-600 transition"
                                      >
                                        Delete
                                      </button>
                                      <button
                                        onClick={() => doReactivate(u.id)}
                                        className="rounded-md bg-gray-900 px-3 py-1.5 text-[12px] font-semibold text-white hover:bg-gray-800 transition"
                                      >
                                        Reactivate
                                      </button>
                                    </div>
                                  </div>
                                )}

                                {/* Deleted users */}
                                {u.status === 'deleted' && (
                                  <div className="border-t border-gray-200 pt-4">
                                    <span className="text-[13px] text-gray-400">
                                      This user has been deleted.
                                    </span>
                                  </div>
                                )}
                              </div>
                            ) : null}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </>
        )}

        {/* ========== Audit Log Tab ========== */}
        {activeTab === 'audit' && (
          <div className="mt-6">
            {/* Action filter */}
            <div className="flex items-center gap-3">
              <select
                value={auditFilter}
                onChange={(e) => {
                  setAuditFilter(e.target.value);
                  setAuditOffset(0);
                }}
                className="rounded-md border border-gray-200 bg-white px-3 py-1.5 text-[13px]"
              >
                <option value="">All actions</option>
                {AUDIT_ACTIONS.map((a) => (
                  <option key={a} value={a}>
                    {a}
                  </option>
                ))}
              </select>
              <span className="text-[12px] text-gray-400 tabular-nums">{auditTotal} entries</span>
            </div>

            {/* Entries */}
            <div className="mt-4">
              {auditLoading ? (
                <div className="flex justify-center py-24">
                  <span className="h-5 w-5 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
                </div>
              ) : auditEntries.length === 0 ? (
                <div className="py-24 text-center">
                  <p className="text-[13px] text-gray-400">No audit log entries.</p>
                </div>
              ) : (
                <div>
                  {auditEntries.map((entry, i) => (
                    <div
                      key={entry.id}
                      className={`py-3 ${i > 0 ? 'border-t border-gray-100' : ''}`}
                      style={{ paddingLeft: 4, paddingRight: 4 }}
                    >
                      <div className="flex items-center gap-3">
                        <span
                          className={`inline-block rounded px-2 py-0.5 text-[11px] font-bold ${
                            entry.success ? 'bg-gray-100 text-gray-700' : 'bg-red-50 text-red-600'
                          }`}
                        >
                          {entry.action}
                        </span>
                        {entry.target_user_id && (
                          <span className="font-mono text-[12px] text-gray-400">
                            {entry.target_user_id}
                          </span>
                        )}
                        <span className="ml-auto text-[12px] text-gray-400">
                          {new Date(entry.timestamp).toLocaleString()}
                        </span>
                      </div>
                      {entry.details && Object.keys(entry.details).length > 0 && (
                        <pre className="mt-1.5 rounded-md bg-gray-50 px-3 py-2 text-[11px] text-gray-600 overflow-x-auto border border-gray-100">
                          {JSON.stringify(entry.details, null, 2)}
                        </pre>
                      )}
                      <div className="mt-1 text-[11px] text-gray-400">from {entry.admin_ip}</div>
                    </div>
                  ))}
                </div>
              )}

              {/* Pagination */}
              {auditTotal > AUDIT_PAGE_SIZE && (
                <div className="mt-4 flex items-center justify-between">
                  <span className="text-[12px] text-gray-400 tabular-nums">
                    {auditOffset + 1}&ndash;{Math.min(auditOffset + AUDIT_PAGE_SIZE, auditTotal)} of{' '}
                    {auditTotal}
                  </span>
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => setAuditOffset(Math.max(0, auditOffset - AUDIT_PAGE_SIZE))}
                      disabled={auditOffset === 0}
                      className="rounded-md px-3 py-1 text-[12px] font-medium text-gray-500 hover:bg-gray-100 transition disabled:opacity-30"
                    >
                      Prev
                    </button>
                    <button
                      onClick={() => setAuditOffset(auditOffset + AUDIT_PAGE_SIZE)}
                      disabled={auditOffset + AUDIT_PAGE_SIZE >= auditTotal}
                      className="rounded-md px-3 py-1 text-[12px] font-medium text-gray-500 hover:bg-gray-100 transition disabled:opacity-30"
                    >
                      Next
                    </button>
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

        {/* ========== Requests Tab ========== */}
        {activeTab === 'requests' && (
          <div className="mt-6">
            {/* Request metrics */}
            <div className="mb-6">
              <div className="mb-3 flex items-center justify-between">
                <div>
                  <h2 className="text-[15px] font-semibold text-gray-900">Request volume</h2>
                  <p className="text-[12px] text-gray-400">
                    Traffic trends across short and long lookback windows.
                  </p>
                </div>
                {reqMetricsLoading && (
                  <span className="h-4 w-4 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
                )}
              </div>
              {reqMetrics.length > 0 ? (
                <div className="grid gap-3 sm:grid-cols-2">
                  {reqMetrics.map((metric) => (
                    <RequestMetricsCard key={metric.key} metric={metric} />
                  ))}
                </div>
              ) : !reqMetricsLoading ? (
                <div className="rounded-xl border border-dashed border-gray-200 py-8 text-center">
                  <p className="text-[13px] text-gray-400">No request metrics available.</p>
                </div>
              ) : null}
            </div>

            {/* Filters */}
            <div className="flex flex-wrap items-center gap-3">
              <input
                type="text"
                value={reqUserFilter}
                onChange={(e) => {
                  setReqUserFilter(e.target.value);
                  setReqOffset(0);
                }}
                placeholder="Filter by user ID..."
                className="flex-1 min-w-[160px] rounded-lg border border-gray-200 bg-white px-4 py-2 text-[13px] placeholder:text-gray-400 focus:border-gray-400 focus:outline-none"
              />
              <input
                type="text"
                value={reqModelFilter}
                onChange={(e) => {
                  setReqModelFilter(e.target.value);
                  setReqOffset(0);
                }}
                placeholder="Filter by model..."
                className="flex-1 min-w-[160px] rounded-lg border border-gray-200 bg-white px-4 py-2 text-[13px] placeholder:text-gray-400 focus:border-gray-400 focus:outline-none"
              />
              <label className="flex items-center gap-1.5 text-[13px] text-gray-600 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={reqErrorsOnly}
                  onChange={(e) => {
                    setReqErrorsOnly(e.target.checked);
                    setReqOffset(0);
                  }}
                  className="rounded border-gray-300"
                />
                Errors only
              </label>
              <span className="text-[12px] text-gray-400 tabular-nums">{reqTotal} entries</span>
            </div>

            {/* Table */}
            <div className="mt-4">
              {reqLoading ? (
                <div className="flex justify-center py-24">
                  <span className="h-5 w-5 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
                </div>
              ) : reqEntries.length === 0 ? (
                <div className="py-24 text-center">
                  <p className="text-[13px] text-gray-400">No requests found.</p>
                </div>
              ) : (
                <div className="-mx-1">
                  <table className="min-w-full">
                    <thead className="bg-gray-50">
                      <tr className="border-b border-gray-200">
                        <th className="py-2 pl-4 pr-3 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500">
                          Model
                        </th>
                        <th className="px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500">
                          User
                        </th>
                        <th className="px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500">
                          IP
                        </th>
                        <th className="px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500">
                          Status
                        </th>
                        <th className="px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500">
                          Latency
                        </th>
                        <th className="px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500">
                          Tokens
                        </th>
                        <th className="px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500">
                          Cost
                        </th>
                        <th className="px-3 py-2 text-right text-[11px] font-medium uppercase tracking-wider text-gray-500 pr-4">
                          Time
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {reqEntries.map((req) => {
                        const isSuccess =
                          req.status_code != null &&
                          req.status_code >= 200 &&
                          req.status_code < 400;
                        const isExpanded = reqExpandedId === req.request_id;
                        return (
                          <Fragment key={req.request_id}>
                            <tr
                              className="border-b border-gray-100 hover:bg-gray-50/60 cursor-pointer transition-colors"
                              onClick={() => setReqExpandedId(isExpanded ? null : req.request_id)}
                            >
                              <td className="whitespace-nowrap py-2.5 pl-4 pr-3 text-[13px]">
                                <div className="font-medium text-gray-900">{req.model_id}</div>
                                <div className="text-[11px] text-gray-400">{req.provider}</div>
                              </td>
                              <td className="whitespace-nowrap px-3 py-2.5 text-[12px] font-mono text-gray-500">
                                {req.user_id ? (
                                  <button
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      setReqUserFilter(req.user_id!);
                                      setReqOffset(0);
                                    }}
                                    className="block max-w-[220px] text-left transition hover:text-gray-900 hover:underline"
                                    title={`${req.user_name || req.user_id}${
                                      req.user_email ? ` <${req.user_email}>` : ''
                                    }`}
                                  >
                                    <span className="block truncate font-sans text-[13px] font-medium text-gray-800">
                                      {req.user_name || req.user_id}
                                    </span>
                                    {req.user_email ? (
                                      <span className="block truncate text-[11px] text-gray-400">
                                        {req.user_email}
                                      </span>
                                    ) : (
                                      <span className="block truncate text-[11px] text-gray-400">
                                        {req.user_id.slice(0, 12)}…
                                      </span>
                                    )}
                                  </button>
                                ) : (
                                  <span className="text-gray-300">—</span>
                                )}
                              </td>
                              <td className="whitespace-nowrap px-3 py-2.5 text-[12px] font-mono text-gray-500">
                                {req.user_ip ?? <span className="text-gray-300">—</span>}
                              </td>
                              <td className="whitespace-nowrap px-3 py-2.5 text-[12px]">
                                {req.status_code != null ? (
                                  <span
                                    className={`inline-flex rounded-full px-2 py-0.5 text-[11px] font-medium ${
                                      isSuccess
                                        ? 'bg-emerald-50 text-emerald-700 ring-1 ring-inset ring-emerald-600/20'
                                        : 'bg-red-50 text-red-700 ring-1 ring-inset ring-red-600/20'
                                    }`}
                                  >
                                    {req.status_code}
                                  </span>
                                ) : (
                                  <span className="text-gray-300">—</span>
                                )}
                              </td>
                              <td className="whitespace-nowrap px-3 py-2.5 text-[12px] tabular-nums text-gray-600">
                                {req.latency_ms != null
                                  ? req.latency_ms >= 1000
                                    ? `${(req.latency_ms / 1000).toFixed(1)}s`
                                    : `${req.latency_ms}ms`
                                  : '—'}
                              </td>
                              <td className="whitespace-nowrap px-3 py-2.5 text-[12px] tabular-nums text-gray-600">
                                {req.prompt_tokens != null || req.completion_tokens != null ? (
                                  <>
                                    <span className="text-gray-400">↑</span>
                                    {(req.prompt_tokens ?? 0).toLocaleString()}
                                    <span className="mx-0.5 text-gray-300">/</span>
                                    <span className="text-gray-400">↓</span>
                                    {(req.completion_tokens ?? 0).toLocaleString()}
                                  </>
                                ) : (
                                  '—'
                                )}
                              </td>
                              <td className="whitespace-nowrap px-3 py-2.5 text-[12px] tabular-nums text-gray-600">
                                {req.cost_usd != null
                                  ? req.cost_usd < 0.01
                                    ? `$${req.cost_usd.toFixed(4)}`
                                    : `$${req.cost_usd.toFixed(2)}`
                                  : '—'}
                              </td>
                              <td className="whitespace-nowrap px-3 py-2.5 text-right text-[12px] text-gray-400 pr-4">
                                <span title={new Date(req.timestamp).toLocaleString()}>
                                  {relTime(req.timestamp)}
                                </span>
                              </td>
                            </tr>
                            {isExpanded && (
                              <tr className="border-b border-gray-100 bg-gray-50/40">
                                <td colSpan={8} className="px-4 py-3">
                                  <div className="grid grid-cols-2 gap-x-8 gap-y-1 text-[11px] sm:grid-cols-4">
                                    <div>
                                      <span className="text-gray-500">Request ID:</span>{' '}
                                      <span className="font-mono text-gray-700">
                                        {req.request_id.length > 24
                                          ? `${req.request_id.slice(0, 24)}…`
                                          : req.request_id}
                                      </span>
                                    </div>
                                    <div>
                                      <span className="text-gray-500">TTFT:</span>{' '}
                                      <span className="text-gray-700">
                                        {req.ttft_ms != null ? `${req.ttft_ms}ms` : '—'}
                                      </span>
                                    </div>
                                    <div>
                                      <span className="text-gray-500">User:</span>{' '}
                                      <span className="text-gray-700">
                                        {req.user_name || req.user_id || '—'}
                                      </span>
                                    </div>
                                    <div>
                                      <span className="text-gray-500">Email:</span>{' '}
                                      <span className="text-gray-700">{req.user_email || '—'}</span>
                                    </div>
                                    <div>
                                      <span className="text-gray-500">User IP:</span>{' '}
                                      <span className="text-gray-700 font-mono">
                                        {req.user_ip ?? '—'}
                                      </span>
                                    </div>
                                    <div>
                                      <span className="text-gray-500">Reasoning:</span>{' '}
                                      <span className="text-gray-700">
                                        {req.reasoning_tokens != null
                                          ? req.reasoning_tokens.toLocaleString()
                                          : '—'}
                                      </span>
                                    </div>
                                    <div>
                                      <span className="text-gray-500">Stream:</span>{' '}
                                      <span className="text-gray-700">
                                        {req.stream != null ? (req.stream ? 'Yes' : 'No') : '—'}
                                      </span>
                                    </div>
                                    <FoldedText label="Prompt" value={req.prompt} />
                                    <FoldedText label="Response" value={req.response} />
                                    {req.error && (
                                      <div className="col-span-full mt-1">
                                        <span className="text-red-600">Error: {req.error}</span>
                                      </div>
                                    )}
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

              {/* Pagination */}
              {reqTotal > REQ_PAGE_SIZE && (
                <div className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                  <span className="text-[12px] text-gray-400 tabular-nums text-center sm:text-left">
                    {reqOffset + 1}&ndash;{Math.min(reqOffset + REQ_PAGE_SIZE, reqTotal)} of{' '}
                    {reqTotal}
                    <span className="ml-2 text-gray-300">
                      (page {Math.floor(reqOffset / REQ_PAGE_SIZE) + 1} of{' '}
                      {Math.ceil(reqTotal / REQ_PAGE_SIZE)})
                    </span>
                  </span>
                  <div className="flex flex-wrap items-center justify-center gap-3 sm:justify-end">
                    <div className="flex items-center gap-2">
                      <button
                        onClick={() => setReqOffset(Math.max(0, reqOffset - REQ_PAGE_SIZE))}
                        disabled={reqOffset === 0}
                        className="rounded-md px-3 py-1 text-[12px] font-medium text-gray-500 hover:bg-gray-100 transition disabled:opacity-30"
                      >
                        Prev
                      </button>
                      <button
                        onClick={() => setReqOffset(reqOffset + REQ_PAGE_SIZE)}
                        disabled={reqOffset + REQ_PAGE_SIZE >= reqTotal}
                        className="rounded-md px-3 py-1 text-[12px] font-medium text-gray-500 hover:bg-gray-100 transition disabled:opacity-30"
                      >
                        Next
                      </button>
                    </div>
                    <div className="flex items-center gap-2">
                      <label
                        htmlFor={reqJumpInputId}
                        className="text-[12px] text-gray-400 whitespace-nowrap"
                      >
                        Jump to page
                      </label>
                      <input
                        id={reqJumpInputId}
                        type="number"
                        min={1}
                        max={Math.ceil(reqTotal / REQ_PAGE_SIZE)}
                        inputMode="numeric"
                        value={reqJumpPage}
                        onChange={(e) => setReqJumpPage(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') {
                            applyOffsetJump(
                              reqJumpPage,
                              reqTotal,
                              REQ_PAGE_SIZE,
                              setReqOffset,
                              () => setReqJumpPage(''),
                            );
                          }
                        }}
                        className="w-14 rounded-md border border-gray-200 px-2 py-1 text-center text-[12px] text-gray-900 tabular-nums focus:border-gray-400 focus:outline-none"
                        aria-label="Page number to jump to"
                      />
                      <button
                        type="button"
                        onClick={() =>
                          applyOffsetJump(reqJumpPage, reqTotal, REQ_PAGE_SIZE, setReqOffset, () =>
                            setReqJumpPage(''),
                          )
                        }
                        className="rounded-md px-3 py-1 text-[12px] font-medium text-gray-500 ring-1 ring-inset ring-gray-200 hover:bg-gray-50"
                      >
                        Go
                      </button>
                    </div>
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

        {/* ========== Broadcast Email Tab ========== */}
        {activeTab === 'broadcast' && (
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
                      setBcBodyHtml('');
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
                      <label className="block text-[12px] font-medium text-gray-600 mb-1">Feature Name</label>
                      <input
                        className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
                        placeholder="e.g. GPT-5 Support"
                        value={bcTemplateVars['feature_name'] ?? ''}
                        onChange={(e) => setBcTemplateVars((v) => ({ ...v, feature_name: e.target.value }))}
                      />
                    </div>
                    <div>
                      <label className="block text-[12px] font-medium text-gray-600 mb-1">Description</label>
                      <textarea
                        rows={3}
                        className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
                        placeholder="Describe the new feature..."
                        value={bcTemplateVars['description'] ?? ''}
                        onChange={(e) => setBcTemplateVars((v) => ({ ...v, description: e.target.value }))}
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
                      <label className="block text-[12px] font-medium text-gray-600 mb-1">Body (HTML)</label>
                      <textarea
                        rows={6}
                        className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] font-mono focus:outline-none focus:ring-2 focus:ring-gray-900"
                        placeholder="<p>Your message here...</p>"
                        value={bcBodyHtml}
                        onChange={(e) => setBcBodyHtml(e.target.value)}
                      />
                    </div>
                  </div>
                )}

                {/* Recipient filters */}
                <div className="mb-4 grid grid-cols-2 gap-6">
                  <div>
                    <label className="block text-[12px] font-medium text-gray-600 mb-2">Roles</label>
                    {['free', 'internal', 'admin'].map((role) => (
                      <label key={role} className="flex items-center gap-2 text-[13px] text-gray-700 mb-1">
                        <input
                          type="checkbox"
                          checked={bcTargetRoles.includes(role)}
                          onChange={(e) =>
                            setBcTargetRoles((prev) =>
                              e.target.checked ? [...prev, role] : prev.filter((r) => r !== role)
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
                      <label key={status} className="flex items-center gap-2 text-[13px] text-gray-700 mb-1">
                        <input
                          type="checkbox"
                          checked={bcTargetStatuses.includes(status)}
                          onChange={(e) =>
                            setBcTargetStatuses((prev) =>
                              e.target.checked ? [...prev, status] : prev.filter((s) => s !== status)
                            )
                          }
                        />
                        {status.replace('_', ' ')}
                      </label>
                    ))}
                  </div>
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
                          body_html: bcBodyHtml,
                          body_text: '',
                          target_roles: bcTargetRoles,
                          target_statuses: bcTargetStatuses,
                        });
                        setBcPreview(res);
                      } catch (err) {
                        setToast(getErrorMessage(err));
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
                          body_html: bcBodyHtml,
                          body_text: '',
                          target_roles: bcTargetRoles,
                          target_statuses: bcTargetStatuses,
                        });
                        setToast('Test email sent to your address');
                      } catch (err) {
                        setToast(getErrorMessage(err));
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
                      {bcPreview.recipient_count} recipient{bcPreview.recipient_count !== 1 ? 's' : ''} match your filters
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
                      {bcPreview ? `This will send to ${bcPreview.recipient_count} recipient(s).` : 'Send broadcast email?'}
                    </p>
                    {bcScheduleMode === 'later' && bcScheduledAt && (
                      <p className="text-[12px] text-gray-500 mb-4">Scheduled for: {new Date(bcScheduledAt).toLocaleString()}</p>
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
                              body_html: bcBodyHtml,
                              body_text: '',
                              target_roles: bcTargetRoles,
                              target_statuses: bcTargetStatuses,
                              scheduled_at:
                                bcScheduleMode === 'later' && bcScheduledAt
                                  ? new Date(bcScheduledAt).toISOString()
                                  : null,
                            });
                            setToast(bcScheduleMode === 'later' ? 'Broadcast scheduled' : 'Broadcast queued');
                            await loadBroadcasts();
                          } catch (err) {
                            setToast(getErrorMessage(err));
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
                                      setToast('Broadcast cancelled');
                                      await loadBroadcasts();
                                    } catch (err) {
                                      setToast(getErrorMessage(err));
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
    </ProtectedRoute>
  );
}
