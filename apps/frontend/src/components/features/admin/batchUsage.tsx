import type { AdminBatchSummary } from '@/lib/api/batches';

/** Requests by outcome; the parts add up to the total. */
export interface OutcomeCounts {
  succeeded: number;
  failed: number;
  expired: number;
  cancelled: number;
  unfinished: number;
  total: number;
}

type OutcomePart = Exclude<keyof OutcomeCounts, 'total'>;

/** The parts in the order the bar draws and the legend lists them. */
const PARTS: readonly {
  key: OutcomePart;
  label: string;
  /** What the part means, for the hover text. */
  help: string;
  swatch: string;
}[] = [
  {
    key: 'succeeded',
    label: 'succeeded',
    help: 'Answered successfully.',
    swatch: 'bg-emerald-500',
  },
  {
    key: 'failed',
    label: 'failed',
    help: 'Ran and returned an error.',
    swatch: 'bg-red-500',
  },
  {
    key: 'expired',
    label: 'expired unrun',
    help: "Never started: the batch's execution window closed first.",
    swatch: 'bg-amber-400',
  },
  {
    key: 'cancelled',
    label: 'cancelled',
    help: 'Stopped by a cancel before they ran.',
    swatch: 'bg-gray-400',
  },
  {
    key: 'unfinished',
    label: 'not finished',
    help: 'Queued or running.',
    swatch: 'bg-blue-300',
  },
];

/**
 * A summary row's requests by outcome, as each batch counts them: the
 * gateway's `failed` includes the expired requests, so they are taken out of it
 * here and shown on their own. A gateway older than the `expired` field leaves
 * them in `failed`.
 */
export function summaryOutcomeCounts(usage: AdminBatchSummary['totals']): OutcomeCounts {
  const expired = usage.expired ?? 0;
  return {
    succeeded: usage.completed,
    failed: Math.max(usage.failed - expired, 0),
    expired,
    cancelled: usage.cancelled,
    unfinished: Math.max(usage.requests - usage.completed - usage.failed - usage.cancelled, 0),
    total: usage.requests,
  };
}

/** A dollar amount: cents once it reaches one, else four decimals so small costs still show. */
export function usd(value: number): string {
  if (value === 0) return '$0.00';
  if (Math.abs(value) < 1) return `$${value.toFixed(4)}`;
  return value.toLocaleString('en-US', { style: 'currency', currency: 'USD' });
}

const COMPACT = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 });

/** A count, exact below 10,000 and shortened above it (12.9K, 4.2M). */
export function compactNumber(value: number): string {
  return Math.abs(value) < 10_000 ? value.toLocaleString('en-US') : COMPACT.format(value);
}

/**
 * `part` as a share of `whole`, to one decimal. Never rounds to 100% while
 * anything is left out, nor to 0% while there is some.
 */
export function sharePercent(part: number, whole: number): string {
  if (whole <= 0) return '—';
  let share = Math.round((part / whole) * 1000) / 10;
  if (part < whole) share = Math.min(share, 99.9);
  if (part > 0) share = Math.max(share, 0.1);
  return `${share.toLocaleString('en-US', { maximumFractionDigits: 1 })}%`;
}

function partText(counts: OutcomeCounts, key: OutcomePart, label: string): string {
  return `${counts[key].toLocaleString('en-US')} ${label}`;
}

/**
 * Requests by outcome: a thin bar split into parts, and a legend naming each
 * part present with its count. The bar's accessible name and each part's
 * hover text give every count and share; the legend makes colour optional.
 */
export function OutcomeBar({
  counts,
  className = '',
}: {
  counts: OutcomeCounts;
  className?: string;
}) {
  const present = PARTS.filter(({ key }) => counts[key] > 0);
  if (counts.total === 0) {
    return <p className={`text-[12px] text-gray-500 ${className}`}>No requests</p>;
  }
  return (
    <div className={className}>
      <div
        role="img"
        aria-label={present.map(({ key, label }) => partText(counts, key, label)).join(', ')}
        className="flex h-2 w-full gap-0.5 overflow-hidden rounded-full"
      >
        {present.map(({ key, label, swatch }) => (
          <span
            key={key}
            className={`h-full min-w-[3px] ${swatch}`}
            style={{ flexGrow: counts[key], flexBasis: 0 }}
            title={`${partText(counts, key, label)} (${sharePercent(counts[key], counts.total)})`}
          />
        ))}
      </div>
      <ul className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[12px] text-gray-600">
        {present.map(({ key, label, help, swatch }) => (
          <li key={key} className="inline-flex items-center gap-1.5" title={help}>
            <span aria-hidden="true" className={`h-2 w-2 shrink-0 rounded-full ${swatch}`} />
            {partText(counts, key, label)}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** One headline figure: a label, the value, and a line of context under it. */
export function StatTile({
  label,
  value,
  valueTitle,
  detail,
  className = '',
  children,
}: {
  label: string;
  value: string;
  /** The exact value, when `value` is shortened. */
  valueTitle?: string;
  detail?: React.ReactNode;
  className?: string;
  children?: React.ReactNode;
}) {
  return (
    <div className={`rounded-lg border border-gray-200 bg-white p-4 ${className}`}>
      <dt className="text-[12px] font-medium text-gray-500">{label}</dt>
      <dd className="mt-1">
        <span className="text-[24px] font-semibold text-gray-900" title={valueTitle}>
          {value}
        </span>
        {detail && <div className="mt-0.5 text-[12px] text-gray-500">{detail}</div>}
        {children}
      </dd>
    </div>
  );
}
