'use client';

import { Fragment, type ReactNode } from 'react';
import { useT } from '@/components/providers/useT';
import { fill } from '@/lib/utils/interpolate';

/** The fields an audit entry stored of its change; none for an entry without details. */
export function auditDetailFields(details: unknown): [string, unknown][] {
  return details && typeof details === 'object' ? Object.entries(details) : [];
}

/** One value on one line, cut short: for the summary of a row. */
export function shortValue(v: unknown, max = 48): string {
  if (v === null || v === undefined) return '—';
  const s = typeof v === 'string' ? v : typeof v === 'object' ? JSON.stringify(v) : String(v);
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

/** A value in full: objects and arrays as indented JSON. */
export function fullValue(v: unknown): string {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'string') return v;
  if (typeof v === 'object') return JSON.stringify(v, null, 2);
  return String(v);
}

/**
 * What an entry changed, in one line. A before/after pair reads
 * `key: old → new`; a list of changed fields reads as that list; anything else
 * shows its first two set fields and how many more there are. Every field is
 * in the entry's details.
 */
export function AuditChangeSummary({ details }: { details: unknown }) {
  const t = useT();
  const fields = auditDetailFields(details);
  if (fields.length === 0) {
    return <span className="text-gray-400">{t('console.audit.no_details', 'No details')}</span>;
  }
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
        <span className="sr-only">{t('console.audit.changed_to', ' changed to ')}</span>
        <span className="text-gray-900">{shortValue(d.new_value)}</span>
      </>
    );
  }

  if (Array.isArray(d.updated_fields) && d.updated_fields.length > 0) {
    return (
      <>
        <span className="text-gray-400">{t('console.audit.changed', 'Changed ')}</span>
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
      {more > 0 ? (
        <span className="pl-1.5 text-gray-400">
          {fill(t('console.audit.more', '+{count} more'), { count: String(more) })}
        </span>
      ) : null}
    </>
  );
}

/** One line of an opened entry: its label, its value, and the classes of the value. */
export type AuditMeta = [label: string, value: ReactNode, valueClass?: string];

/**
 * An audit entry, opened: the lines the caller chose to show of the record
 * (who, what, when), then every field of the change in full, then the stored
 * JSON. The caller decides which lines a reader may see; a field of the
 * details is shown as it was stored.
 */
export function AuditEntryDetails({
  meta,
  details,
  raw = true,
}: {
  meta: AuditMeta[];
  details: unknown;
  /** Offer the stored JSON under the fields. */
  raw?: boolean;
}) {
  const t = useT();
  const fields = auditDetailFields(details);
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
      {raw && details != null ? (
        <details>
          <summary className="w-fit cursor-pointer rounded text-[11px] font-medium text-gray-500 transition-colors duration-150 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400">
            {t('console.audit.raw_json', 'Raw JSON')}
          </summary>
          <pre className="mt-1.5 max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-md border border-gray-200 bg-white px-3 py-2 text-[11px] text-gray-600">
            {JSON.stringify(details, null, 2)}
          </pre>
        </details>
      ) : null}
    </div>
  );
}

/** The mark of a row that opens: it points right, and down when the row is open. */
export function DisclosureChevron({ open }: { open: boolean }) {
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
