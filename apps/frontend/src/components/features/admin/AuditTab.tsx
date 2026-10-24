'use client';

import { Fragment, useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import { Select } from '@/components/ui/Select';
import { AuditLogEntry, listAuditLog } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import {
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
  AdminSection,
  AdminSpinner,
} from './AdminPrimitives';
import {
  BUTTON_GHOST,
  BUTTON_SECONDARY,
  CONTROL,
  NOTICE_ACTION,
  PANEL,
  TABLE_HEAD_CELL,
} from './adminStyles';

const AUDIT_PAGE_SIZE = 50;

// The actions the filter offers. The log also records others (settings,
// configuration, alerts, ...); "All actions" shows those, and a filter set by
// a link is offered as it is (see the select below).
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

/**
 * A readable name for an action: `update_user` -> "Update user",
 * `settings.update` -> "Settings update". The stored name stays available in
 * the entry's details.
 */
function actionLabel(action: string): string {
  if (!action) return action;
  const s = action.replace(/[._]/g, ' ');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

type AuditCategory = 'create' | 'approve' | 'reject' | 'update' | 'delete' | 'other';

function actionCategory(stored: string): AuditCategory {
  // `settings.update` has the same verb as `key_update`.
  const action = stored.replace(/\./g, '_');
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

function formatShortDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** One value on one line, cut short: for the row summary. */
function shortValue(v: unknown, max = 48): string {
  if (v === null || v === undefined) return '—';
  const s = typeof v === 'string' ? v : typeof v === 'object' ? JSON.stringify(v) : String(v);
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

/** A value in full: objects and arrays as indented JSON. */
function fullValue(v: unknown): string {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'string') return v;
  if (typeof v === 'object') return JSON.stringify(v, null, 2);
  return String(v);
}

function detailEntries(entry: AuditLogEntry): [string, unknown][] {
  return entry.details && typeof entry.details === 'object' ? Object.entries(entry.details) : [];
}

/**
 * What changed, in one line. A before/after pair reads `key: old → new`; a
 * list of changed fields reads as that list; anything else shows its first
 * two set fields and how many more there are. Every field is in the details.
 */
function DetailSummary({ entry }: { entry: AuditLogEntry }) {
  const fields = detailEntries(entry);
  if (fields.length === 0) return <span className="text-gray-400">No details</span>;
  const d = Object.fromEntries(fields);

  if ('old_value' in d || 'new_value' in d) {
    return (
      <>
        {typeof d.key === 'string' ? (
          <span className="font-mono text-[12px] text-gray-900">{d.key}</span>
        ) : null}
        {typeof d.key === 'string' ? <span className="text-gray-400">: </span> : null}
        <span className="text-gray-500">{shortValue(d.old_value)}</span>
        <span className="px-1 text-gray-400" aria-hidden="true">
          →
        </span>
        <span className="sr-only"> changed to </span>
        <span className="text-gray-900">{shortValue(d.new_value)}</span>
      </>
    );
  }

  if (Array.isArray(d.updated_fields) && d.updated_fields.length > 0) {
    return (
      <>
        <span className="text-gray-400">Changed </span>
        <span className="text-gray-900">{d.updated_fields.map(String).join(', ')}</span>
      </>
    );
  }

  const set = fields.filter(([, v]) => v !== null && v !== undefined && v !== '');
  const shown = (set.length > 0 ? set : fields).slice(0, 2);
  const more = fields.length - shown.length;
  return (
    <>
      {shown.map(([k, v], i) => (
        <Fragment key={k}>
          {i > 0 ? <span className="px-1.5 text-gray-300">·</span> : null}
          <span className="text-gray-400">{k} </span>
          <span className="text-gray-900">{shortValue(v)}</span>
        </Fragment>
      ))}
      {more > 0 ? <span className="pl-1.5 text-gray-400">+{more} more</span> : null}
    </>
  );
}

/** The whole record, every field in full, then the stored JSON. */
function EntryDetails({ entry }: { entry: AuditLogEntry }) {
  const fields = detailEntries(entry);
  // The row already reads the local time, the action and the user. Here each
  // stored field appears once, exactly as recorded.
  const meta: [string, ReactNode, string?][] = [
    ['Action', entry.action, 'font-mono'],
    [
      'Timestamp',
      <time key="t" dateTime={entry.timestamp}>
        {entry.timestamp}
      </time>,
      'font-mono',
    ],
    [
      'Outcome',
      <span key="o" className={entry.success ? undefined : 'font-medium text-red-700'}>
        {entry.success ? 'Succeeded' : 'Failed'}
      </span>,
    ],
    ['Target user', entry.target_user_id ?? '—', entry.target_user_id ? 'font-mono' : undefined],
    ['Source', entry.admin_ip, 'font-mono'],
  ];
  return (
    <div className="animate-admin-reveal space-y-2.5">
      <dl className="flex flex-wrap gap-x-6 gap-y-2">
        {meta.map(([label, value, valueClass]) => (
          <div key={label} className="min-w-0 max-w-full">
            <dt className="text-[11px] leading-4 text-gray-400">{label}</dt>
            <dd
              className={`text-[12px] leading-5 text-gray-800 [overflow-wrap:anywhere] ${valueClass ?? ''}`}
            >
              {value}
            </dd>
          </div>
        ))}
      </dl>
      {fields.length > 0 ? (
        <dl className="grid grid-cols-[minmax(0,max-content)_minmax(0,1fr)] gap-x-4 gap-y-1 rounded-md border border-gray-200 bg-white px-3 py-2 font-mono text-[12px] leading-5">
          {fields.map(([k, v]) => (
            <Fragment key={k}>
              <dt className="max-w-[16rem] text-gray-500 [overflow-wrap:anywhere]">{k}</dt>
              <dd className="min-w-0 whitespace-pre-wrap text-gray-800 [overflow-wrap:anywhere]">
                {fullValue(v)}
              </dd>
            </Fragment>
          ))}
        </dl>
      ) : null}
      {entry.details != null ? (
        <details>
          <summary className="w-fit cursor-pointer rounded text-[11px] font-medium text-gray-500 transition-colors duration-150 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400">
            Raw JSON
          </summary>
          <pre className="mt-1.5 max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-md border border-gray-200 bg-white px-3 py-2 text-[11px] text-gray-600">
            {JSON.stringify(entry.details, null, 2)}
          </pre>
        </details>
      ) : null}
    </div>
  );
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      viewBox="0 0 20 20"
      fill="currentColor"
      aria-hidden="true"
      className={`h-3.5 w-3.5 transition-transform duration-150 motion-reduce:transition-none ${
        open ? 'rotate-90' : ''
      }`}
    >
      <path
        fillRule="evenodd"
        d="M7.21 14.77a.75.75 0 01.02-1.06L11.168 10 7.23 6.29a.75.75 0 111.04-1.08l4.5 4.25a.75.75 0 010 1.08l-4.5 4.25a.75.75 0 01-1.06-.02z"
        clipRule="evenodd"
      />
    </svg>
  );
}

export function AuditTab() {
  const toast = useAccountToast();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const baseId = useId();
  const actionFilterId = `${baseId}-action`;
  const userFilterId = `${baseId}-user`;

  const auditFilter = searchParams.get('action') ?? '';
  const auditUserFilter = searchParams.get('user_id') ?? '';
  const auditOffset = Number.parseInt(searchParams.get('offset') ?? '0', 10) || 0;
  const filtered = auditFilter !== '' || auditUserFilter !== '';

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

  // The page on screen, with the query that produced it. When the filters or
  // offset change, these rows belong to the old query until the new one
  // answers: they are dimmed (without count or range) while it loads and
  // withdrawn if it fails, never shown as the new query's results.
  const queryKey = `${auditFilter}\n${auditUserFilter}\n${auditOffset}`;
  const [shown, setShown] = useState<{
    key: string;
    entries: AuditLogEntry[];
    total: number;
    offset: number;
  } | null>(null);
  const [auditLoading, setAuditLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<ReadonlySet<number>>(new Set());
  const auditReqIdRef = useRef(0);

  const loadAudit = useCallback(async () => {
    const reqId = ++auditReqIdRef.current;
    setAuditLoading(true);
    // A failure is its own request's: its alert goes when the next one starts.
    setLoadError(null);
    try {
      const d = await listAuditLog(
        auditFilter || undefined,
        auditUserFilter || undefined,
        AUDIT_PAGE_SIZE,
        auditOffset,
      );
      if (auditReqIdRef.current !== reqId) return;
      setShown({ key: queryKey, entries: d.entries, total: d.total, offset: auditOffset });
    } catch (e) {
      if (auditReqIdRef.current !== reqId) return;
      const message = getErrorMessage(e);
      setLoadError(message);
      toast.error(message);
    } finally {
      if (auditReqIdRef.current === reqId) setAuditLoading(false);
    }
  }, [auditFilter, auditUserFilter, auditOffset, queryKey, toast]);

  useEffect(() => {
    loadAudit();
  }, [loadAudit]);

  const toggle = (id: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const current = shown !== null && shown.key === queryKey;
  // Rows of an earlier query stay (dimmed) only while the new one loads.
  const visible = current || auditLoading ? shown : null;
  const auditEntries = visible?.entries ?? [];
  const auditTotal = current ? shown.total : 0;
  const shownOffset = current ? shown.offset : 0;
  const pending = auditLoading || !current;
  const hasPages = current && auditTotal > AUDIT_PAGE_SIZE;
  const clearFilters = () => setQueryParams({ action: null, user_id: null, offset: null });

  return (
    <AdminSection title="Audit log" description="Changes made by administrators, newest first.">
      {/* Filters */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <label htmlFor={actionFilterId} className="sr-only">
          Filter by action
        </label>
        <Select
          id={actionFilterId}
          value={auditFilter}
          onValueChange={(next) => setQueryParams({ action: next || null, offset: null })}
          options={[
            { value: '', label: 'All actions' },
            {
              label: 'Users and API keys',
              options: AUDIT_ACTIONS.map((a) => ({ value: a, label: actionLabel(a) })),
            },
            // A filter from a link that is not in the list above: show it,
            // rather than claim "All actions" while it applies.
            ...(auditFilter && !AUDIT_ACTIONS.includes(auditFilter)
              ? [
                  {
                    label: 'Current filter',
                    options: [{ value: auditFilter, label: actionLabel(auditFilter) }],
                  },
                ]
              : []),
          ]}
          searchable={false}
          className="w-48"
        />
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
          spellCheck={false}
          className={`${CONTROL} w-full min-w-0 font-mono sm:w-72`}
        />
        {filtered ? (
          <button type="button" onClick={clearFilters} className={BUTTON_GHOST}>
            Clear filters
          </button>
        ) : null}
        <span
          className="ml-auto inline-flex items-center gap-2 text-[12px] tabular-nums text-gray-500"
          aria-live="polite"
        >
          {auditLoading && shown ? <AdminSpinner size="sm" /> : null}
          {auditLoading && shown
            ? 'Loading…'
            : current
              ? `${auditTotal} ${filtered ? 'matching ' : ''}entries`
              : null}
        </span>
      </div>

      {loadError ? (
        <div className="mb-3">
          <AdminNotice
            role="alert"
            action={
              <button type="button" onClick={() => void loadAudit()} className={NOTICE_ACTION}>
                Retry
              </button>
            }
          >
            The audit log could not be loaded: {loadError}
          </AdminNotice>
        </div>
      ) : null}

      {visible || auditLoading ? (
        <div className={PANEL}>
          {!visible ? (
            <AdminLoading label="Loading audit log…" />
          ) : auditEntries.length === 0 ? (
            current ? (
              <AdminEmptyState
                variant="plain"
                title={filtered ? 'No entries match these filters.' : 'No audit log entries yet.'}
              >
                {filtered ? (
                  <button type="button" onClick={clearFilters} className={`${NOTICE_ACTION} mt-1`}>
                    Clear filters
                  </button>
                ) : null}
              </AdminEmptyState>
            ) : null
          ) : (
            // One line per entry on a desktop; on a phone the table scrolls
            // inside its box instead of widening the page. While a new page or
            // filter loads, the current rows stay (dimmed) instead of blanking.
            <div
              className={`relative overflow-x-auto rounded-lg transition-opacity duration-150 ${
                pending ? 'opacity-60' : ''
              }`}
              aria-busy={pending || undefined}
            >
              <table className="w-full min-w-[52rem] table-fixed text-[13px]">
                <colgroup>
                  <col className="w-[8.5rem]" />
                  <col className="w-[11.5rem]" />
                  <col />
                  <col className="w-[8.5rem]" />
                  <col className="w-[8rem]" />
                  <col className="w-12" />
                </colgroup>
                <thead className="bg-gray-50/80">
                  <tr className="border-b border-gray-200">
                    <th className={TABLE_HEAD_CELL}>Time</th>
                    <th className={TABLE_HEAD_CELL}>Action</th>
                    <th className={TABLE_HEAD_CELL}>Change</th>
                    <th className={TABLE_HEAD_CELL}>User</th>
                    <th className={`${TABLE_HEAD_CELL} text-right`}>Source</th>
                    <th className={TABLE_HEAD_CELL}>
                      <span className="sr-only">Details</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {auditEntries.map((entry) => {
                    const cat = actionCategory(entry.action);
                    const badgeClass = entry.success
                      ? AUDIT_CATEGORY_CLASS[cat]
                      : 'bg-red-50 text-red-700';
                    const absoluteTs = new Date(entry.timestamp).toLocaleString();
                    const tid = entry.target_user_id;
                    const tidDisplay = tid && tid.length > 12 ? `${tid.slice(0, 8)}…` : tid;
                    const isOpen = expanded.has(entry.id);
                    const detailsId = `${baseId}-entry-${entry.id}`;
                    const knownSource = entry.admin_ip && entry.admin_ip !== 'unknown';
                    return (
                      <Fragment key={entry.id}>
                        <tr
                          className={`border-b border-gray-100 transition-colors duration-150 ${
                            entry.success ? 'hover:bg-gray-50/60' : 'bg-red-50/50'
                          } ${isOpen ? 'border-b-0 bg-gray-50/60' : ''}`}
                        >
                          <td className="px-4 py-2.5 align-top">
                            <time
                              dateTime={entry.timestamp}
                              title={absoluteTs}
                              aria-label={absoluteTs}
                              className="block whitespace-nowrap text-[12px] tabular-nums text-gray-700"
                            >
                              {formatRelative(entry.timestamp)}
                            </time>
                            <span
                              aria-hidden="true"
                              className="block whitespace-nowrap text-[11px] tabular-nums text-gray-400"
                            >
                              {formatShortDate(entry.timestamp)}
                            </span>
                          </td>
                          <td className="px-4 py-2.5 align-top">
                            <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                              {!entry.success && (
                                <span className="text-[10px] font-bold uppercase tracking-wide text-red-700">
                                  FAILED
                                </span>
                              )}
                              <span
                                title={entry.action}
                                className={`inline-block max-w-full truncate rounded px-1.5 py-0.5 text-[12px] font-medium ${badgeClass}`}
                              >
                                {actionLabel(entry.action)}
                              </span>
                            </div>
                          </td>
                          <td className="px-4 py-2.5 align-top">
                            <div className="truncate text-[12px] leading-5">
                              <DetailSummary entry={entry} />
                            </div>
                          </td>
                          <td className="px-4 py-2.5 align-top">
                            {tid ? (
                              <button
                                type="button"
                                onClick={() => {
                                  setQueryParams({ user_id: tid, offset: null });
                                }}
                                title={tid}
                                aria-label={`Filter by user ${tid}`}
                                className="max-w-full truncate rounded font-mono text-[12px] text-gray-700 transition-colors duration-150 hover:text-gray-900 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400"
                              >
                                <span aria-hidden="true">{tidDisplay}</span>
                                <span className="sr-only">{tid}</span>
                              </button>
                            ) : (
                              <span className="text-gray-300">—</span>
                            )}
                          </td>
                          <td
                            className={`truncate px-4 py-2.5 text-right align-top font-mono text-[12px] ${
                              knownSource ? 'text-gray-500' : 'text-gray-400'
                            }`}
                            title={entry.admin_ip}
                          >
                            {entry.admin_ip}
                          </td>
                          <td className="py-2 pr-3 text-right align-top">
                            <button
                              type="button"
                              onClick={() => toggle(entry.id)}
                              aria-expanded={isOpen}
                              aria-controls={isOpen ? detailsId : undefined}
                              aria-label={`${isOpen ? 'Hide' : 'Show'} details of ${actionLabel(entry.action)}, ${absoluteTs}`}
                              className="inline-flex h-7 w-7 items-center justify-center rounded-md text-gray-400 transition-colors duration-150 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400"
                            >
                              <Chevron open={isOpen} />
                            </button>
                          </td>
                        </tr>
                        {isOpen ? (
                          <tr
                            id={detailsId}
                            className={`border-b border-gray-100 ${
                              entry.success ? 'bg-gray-50/60' : 'bg-red-50/50'
                            }`}
                          >
                            <td colSpan={6} className="px-4 pb-4 pt-1">
                              <EntryDetails entry={entry} />
                            </td>
                          </tr>
                        ) : null}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {/* Pagination */}
          {hasPages && (
            <div className="flex flex-wrap items-center justify-between gap-2 border-t border-gray-200 px-4 py-2.5">
              <span className="text-[12px] tabular-nums text-gray-500">
                {shownOffset + 1}&ndash;{Math.min(shownOffset + AUDIT_PAGE_SIZE, auditTotal)} of{' '}
                {auditTotal}
              </span>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() =>
                    setQueryParams({
                      offset: String(Math.max(0, shownOffset - AUDIT_PAGE_SIZE)),
                    })
                  }
                  disabled={auditLoading || shownOffset === 0}
                  className={BUTTON_SECONDARY}
                >
                  Prev
                </button>
                <button
                  type="button"
                  onClick={() => setQueryParams({ offset: String(shownOffset + AUDIT_PAGE_SIZE) })}
                  disabled={auditLoading || shownOffset + AUDIT_PAGE_SIZE >= auditTotal}
                  className={BUTTON_SECONDARY}
                >
                  Next
                </button>
              </div>
            </div>
          )}
        </div>
      ) : null}
    </AdminSection>
  );
}
