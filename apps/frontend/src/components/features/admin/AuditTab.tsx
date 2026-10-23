'use client';

import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import { AuditLogEntry, listAuditLog } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { AdminEmptyState, AdminLoading, AdminSpinner } from './AdminPrimitives';
import { BUTTON_SECONDARY, CARD, CARD_TOOLBAR, CONTROL } from './adminStyles';

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
      <summary className="mt-1 w-fit cursor-pointer rounded text-[11px] text-gray-400 hover:text-gray-600">
        Raw JSON
      </summary>
      {open && (
        <pre className="mt-1.5 max-h-80 overflow-auto rounded-md border border-gray-100 bg-gray-50 px-3 py-2 text-[11px] text-gray-600">
          {JSON.stringify(data, null, 2)}
        </pre>
      )}
    </details>
  );
}

export function AuditTab() {
  const toast = useAccountToast();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const actionFilterId = useId();
  const userFilterId = useId();

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
  }, [auditFilter, auditUserFilter, auditOffset, toast]);

  useEffect(() => {
    loadAudit();
  }, [loadAudit]);

  const hasPages = auditTotal > AUDIT_PAGE_SIZE;

  return (
    <div className={CARD}>
      {/* Filters */}
      <div className={CARD_TOOLBAR}>
        <label htmlFor={actionFilterId} className="sr-only">
          Filter by action
        </label>
        <select
          id={actionFilterId}
          value={auditFilter}
          onChange={(e) => {
            setQueryParams({ action: e.target.value || null, offset: null });
          }}
          className={CONTROL}
        >
          <option value="">All actions</option>
          {AUDIT_ACTIONS.map((a) => (
            <option key={a} value={a}>
              {actionLabel(a)}
            </option>
          ))}
        </select>
        <label htmlFor={userFilterId} className="sr-only">
          Filter by user ID
        </label>
        <input
          id={userFilterId}
          type="text"
          value={auditUserFilter}
          onChange={(e) => {
            setQueryParams({ user_id: e.target.value || null, offset: null });
          }}
          placeholder="Filter by user ID…"
          className={`${CONTROL} min-w-[12rem] flex-1 font-mono sm:max-w-sm`}
        />
        <span className="ml-auto inline-flex items-center gap-2 text-[12px] tabular-nums text-gray-500">
          {auditLoading && <AdminSpinner size="sm" />}
          {auditTotal} entries
        </span>
      </div>

      {/* Entries */}
      {auditLoading ? (
        <AdminLoading />
      ) : auditEntries.length === 0 ? (
        <AdminEmptyState variant="plain" title="No audit log entries." />
      ) : (
        <ul className="divide-y divide-gray-100">
          {auditEntries.map((entry) => {
            const cat = actionCategory(entry.action);
            const badgeClass = entry.success ? AUDIT_CATEGORY_CLASS[cat] : 'bg-red-50 text-red-700';
            const absoluteTs = new Date(entry.timestamp).toLocaleString();
            const tid = entry.target_user_id;
            const tidDisplay = tid && tid.length > 12 ? `${tid.slice(0, 8)}…` : tid;
            const detailEntries =
              entry.details && typeof entry.details === 'object'
                ? Object.entries(entry.details)
                : [];
            return (
              <li
                key={entry.id}
                className={`px-4 py-3 ${entry.success ? '' : 'border-l-2 border-l-rose-300 pl-3.5'}`}
              >
                {/* One line per column on wide screens: what happened, its
                    details, then when and from where. Stacked when narrow. */}
                <div className="flex flex-col gap-1.5 lg:grid lg:grid-cols-[minmax(0,16rem)_minmax(0,1fr)_auto] lg:items-start lg:gap-4">
                  <div className="flex flex-wrap items-center gap-2">
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
                        type="button"
                        onClick={() => {
                          setQueryParams({ user_id: tid, offset: null });
                        }}
                        title={tid}
                        aria-label={`Filter by user ${tid}`}
                        className="rounded font-mono text-[12px] text-gray-500 hover:text-gray-900 hover:underline"
                      >
                        <span aria-hidden="true">{tidDisplay}</span>
                        <span className="sr-only">{tid}</span>
                      </button>
                    )}
                  </div>
                  <div className="min-w-0">
                    {detailEntries.length > 0 && (
                      <>
                        <div className="flex flex-wrap gap-1.5">
                          {detailEntries.map(([k, v]) => {
                            const { display, full } = formatAuditDetailValue(v);
                            return (
                              <span
                                key={k}
                                title={full}
                                className="inline-flex max-w-full items-center gap-1 rounded border border-gray-100 bg-gray-50 px-1.5 py-0.5 text-[11px] text-gray-700"
                              >
                                <span className="shrink-0 text-gray-400">{k}:</span>
                                <span className="min-w-0 break-all">{display}</span>
                              </span>
                            );
                          })}
                        </div>
                        <RawJsonDetails data={entry.details} />
                      </>
                    )}
                  </div>
                  <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-[11px] text-gray-400 lg:flex-col lg:items-end lg:text-right">
                    <time
                      dateTime={entry.timestamp}
                      title={absoluteTs}
                      aria-label={absoluteTs}
                      className="whitespace-nowrap text-[12px] text-gray-500"
                    >
                      {formatRelative(entry.timestamp)}
                    </time>
                    <span className="whitespace-nowrap font-mono">from {entry.admin_ip}</span>
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {/* Pagination */}
      {hasPages && (
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-gray-200 px-4 py-2.5">
          <span className="text-[12px] tabular-nums text-gray-500">
            {auditOffset + 1}&ndash;{Math.min(auditOffset + AUDIT_PAGE_SIZE, auditTotal)} of{' '}
            {auditTotal}
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() =>
                setQueryParams({
                  offset: String(Math.max(0, auditOffset - AUDIT_PAGE_SIZE)),
                })
              }
              disabled={auditOffset === 0}
              className={BUTTON_SECONDARY}
            >
              Prev
            </button>
            <button
              type="button"
              onClick={() => setQueryParams({ offset: String(auditOffset + AUDIT_PAGE_SIZE) })}
              disabled={auditOffset + AUDIT_PAGE_SIZE >= auditTotal}
              className={BUTTON_SECONDARY}
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
