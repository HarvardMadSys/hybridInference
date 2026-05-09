'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import toast from 'react-hot-toast';
import { AuditLogEntry, listAuditLog } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

const AUDIT_PAGE_SIZE = 50;

const AUDIT_ACTIONS = [
  'create_user',
  'approve_user',
  'reject_user',
  'update_user',
  'delete_user',
  'resume_user',
  'hard_delete_user',
  'create_key',
  'revoke_key',
  'delete_key',
  'hard_delete_key',
  'regenerate_key',
  'update_key',
];

function actionLabel(action: string): string {
  if (!action) return action;
  const s = action.replace(/_/g, ' ');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

type AuditCategory = 'create' | 'approve' | 'reject' | 'update' | 'delete' | 'other';

function actionCategory(action: string): AuditCategory {
  if (action === 'regenerate_key') return 'create';
  if (
    action.startsWith('hard_delete') ||
    action.startsWith('revoke_') ||
    action.endsWith('_revoke')
  )
    return 'delete';
  if (action.startsWith('approve_') || action.endsWith('_approve')) return 'approve';
  if (action.startsWith('reject_') || action.endsWith('_reject') || action.endsWith('_cancel'))
    return 'reject';
  if (action.startsWith('create_') || action.endsWith('_create')) return 'create';
  if (action.startsWith('update_') || action.endsWith('_update')) return 'update';
  if (action.startsWith('delete_') || action.endsWith('_delete')) return 'delete';
  return 'other';
}

const AUDIT_CATEGORY_CLASS: Record<AuditCategory, string> = {
  create: 'bg-emerald-50 text-emerald-700',
  approve: 'bg-violet-50 text-violet-700',
  reject: 'bg-amber-50 text-amber-700',
  update: 'bg-sky-50 text-sky-700',
  delete: 'bg-rose-50 text-rose-700',
  other: 'bg-gray-100 text-gray-700',
};

function formatRelative(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${Math.max(s, 0)}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function formatAuditDetailValue(v: unknown): { display: string; full: string } {
  if (v === null || v === undefined) return { display: '—', full: '—' };
  if (typeof v === 'string') {
    const full = v;
    const display = v.length > 80 ? `${v.slice(0, 80)}…` : v;
    return { display, full };
  }
  if (typeof v === 'number' || typeof v === 'boolean') {
    const s = String(v);
    return { display: s, full: s };
  }
  const full = JSON.stringify(v);
  const display = full.length > 80 ? `${full.slice(0, 80)}…` : full;
  return { display, full };
}

function RawJsonDetails({ data }: { data: unknown }) {
  const [open, setOpen] = useState(false);
  return (
    <details onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary className="text-[11px] text-gray-400 cursor-pointer hover:text-gray-600 mt-1">
        Raw JSON
      </summary>
      {open && (
        <pre className="mt-1.5 rounded-md bg-gray-50 px-3 py-2 text-[11px] text-gray-600 overflow-x-auto border border-gray-100">
          {JSON.stringify(data, null, 2)}
        </pre>
      )}
    </details>
  );
}

export function AuditTab() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const auditFilter = searchParams.get('action') ?? '';
  const auditUserFilter = searchParams.get('user_id') ?? '';
  const auditOffset = Number.parseInt(searchParams.get('offset') ?? '0', 10) || 0;

  const setQueryParams = useCallback(
    (updates: Record<string, string | null>) => {
      const params = new URLSearchParams(searchParams.toString());
      for (const [key, value] of Object.entries(updates)) {
        if (value === null || value === '') params.delete(key);
        else params.set(key, value);
      }
      const qs = params.toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [pathname, router, searchParams],
  );

  const [auditEntries, setAuditEntries] = useState<AuditLogEntry[]>([]);
  const [auditTotal, setAuditTotal] = useState(0);
  const [auditLoading, setAuditLoading] = useState(false);
  const auditReqIdRef = useRef(0);

  const loadAudit = useCallback(async () => {
    const reqId = ++auditReqIdRef.current;
    setAuditLoading(true);
    try {
      const d = await listAuditLog(
        auditFilter || undefined,
        auditUserFilter || undefined,
        AUDIT_PAGE_SIZE,
        auditOffset,
      );
      if (auditReqIdRef.current !== reqId) return;
      setAuditEntries(d.entries);
      setAuditTotal(d.total);
    } catch (e) {
      if (auditReqIdRef.current !== reqId) return;
      toast.error(getErrorMessage(e));
    } finally {
      if (auditReqIdRef.current === reqId) setAuditLoading(false);
    }
  }, [auditFilter, auditUserFilter, auditOffset]);

  useEffect(() => {
    loadAudit();
  }, [loadAudit]);

  return (
    <div className="mt-6">
      {/* Action filter */}
      <div className="flex flex-wrap items-center gap-3">
        <select
          value={auditFilter}
          onChange={(e) => {
            setQueryParams({ action: e.target.value || null, offset: null });
          }}
          className="rounded-md border border-gray-200 bg-white px-3 py-1.5 text-[13px]"
        >
          <option value="">All actions</option>
          {AUDIT_ACTIONS.map((a) => (
            <option key={a} value={a}>
              {actionLabel(a)}
            </option>
          ))}
        </select>
        <input
          type="text"
          value={auditUserFilter}
          onChange={(e) => {
            setQueryParams({ user_id: e.target.value || null, offset: null });
          }}
          placeholder="Filter by user ID…"
          className="min-w-[180px] rounded-lg border border-gray-200 bg-white px-4 py-2 text-[13px] placeholder:text-gray-400 focus:border-gray-400 focus:outline-none"
        />
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
            {auditEntries.map((entry, i) => {
              const cat = actionCategory(entry.action);
              const badgeClass = entry.success
                ? AUDIT_CATEGORY_CLASS[cat]
                : 'bg-red-50 text-red-700';
              const rowClass = [
                'py-3',
                i > 0 ? 'border-t border-gray-100' : '',
                !entry.success ? 'border-l-2 border-rose-300 pl-3' : '',
              ]
                .filter(Boolean)
                .join(' ');
              const absoluteTs = new Date(entry.timestamp).toLocaleString();
              const tid = entry.target_user_id;
              const tidDisplay = tid && tid.length > 12 ? `${tid.slice(0, 8)}…` : tid;
              const detailEntries =
                entry.details && typeof entry.details === 'object'
                  ? Object.entries(entry.details)
                  : [];
              return (
                <div
                  key={entry.id}
                  className={rowClass}
                  style={entry.success ? { paddingLeft: 4, paddingRight: 4 } : { paddingRight: 4 }}
                >
                  <div className="flex items-center gap-3">
                    {!entry.success && (
                      <span className="inline-block rounded bg-red-100 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-red-700">
                        FAILED
                      </span>
                    )}
                    <span
                      className={`inline-block rounded px-2 py-0.5 text-[11px] font-bold ${badgeClass}`}
                    >
                      {actionLabel(entry.action)}
                    </span>
                    {tid && (
                      <button
                        onClick={() => {
                          setQueryParams({ user_id: tid, offset: null });
                        }}
                        title={tid}
                        aria-label={`Filter by user ${tid}`}
                        className="font-mono text-[12px] text-gray-500 hover:text-gray-900 hover:underline"
                      >
                        <span aria-hidden="true">{tidDisplay}</span>
                        <span className="sr-only">{tid}</span>
                      </button>
                    )}
                    <time
                      dateTime={entry.timestamp}
                      title={absoluteTs}
                      aria-label={absoluteTs}
                      className="ml-auto text-[12px] text-gray-400"
                    >
                      {formatRelative(entry.timestamp)}
                    </time>
                  </div>
                  {detailEntries.length > 0 && (
                    <>
                      <div className="flex flex-wrap gap-1.5 mt-1.5">
                        {detailEntries.map(([k, v]) => {
                          const { display, full } = formatAuditDetailValue(v);
                          return (
                            <span
                              key={k}
                              title={full}
                              className="inline-flex items-center gap-1 rounded bg-gray-50 border border-gray-100 px-1.5 py-0.5 text-[11px] text-gray-700"
                            >
                              <span className="text-gray-400">{k}:</span>
                              <span>{display}</span>
                            </span>
                          );
                        })}
                      </div>
                      <RawJsonDetails data={entry.details} />
                    </>
                  )}
                  <div className="mt-1 text-[11px] text-gray-400">from {entry.admin_ip}</div>
                </div>
              );
            })}
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
                onClick={() =>
                  setQueryParams({
                    offset: String(Math.max(0, auditOffset - AUDIT_PAGE_SIZE)),
                  })
                }
                disabled={auditOffset === 0}
                className="rounded-md px-3 py-1 text-[12px] font-medium text-gray-500 hover:bg-gray-100 transition disabled:opacity-30"
              >
                Prev
              </button>
              <button
                onClick={() => setQueryParams({ offset: String(auditOffset + AUDIT_PAGE_SIZE) })}
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
  );
}
