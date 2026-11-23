'use client';

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { useT } from '@/components/providers/useT';
import { BUTTON_SECONDARY, FIELD_LABEL, PANEL } from '@/components/features/admin/adminStyles';
import type { MemberLimitSource, OrganizationPeriod } from '@/lib/api/organization';
import { useFormatter } from '@/lib/i18n/useFormatter';
import { fill } from '@/lib/utils/interpolate';
import { PERIODS, limitUse, periodLabel, periodRange } from './text';

/**
 * The small pieces the organization console's pages are built from. They use
 * the admin console's class strings, so the two areas look alike and the dark
 * theme repaints both; every word goes through `t()`, because this area is for
 * an organization's own staff and follows the reader's language.
 */

/* -- the period every page of the area reads ------------------------------- */

interface PeriodState {
  period: OrganizationPeriod;
  setPeriod: (period: OrganizationPeriod) => void;
}

const PeriodContext = createContext<PeriodState | null>(null);

/** Keeps one period for the whole area, so it survives a change of tab. */
export function OrganizationPeriodProvider({ children }: { children: ReactNode }) {
  const [period, setPeriod] = useState<OrganizationPeriod>('7d');
  const value = useMemo(() => ({ period, setPeriod }), [period]);
  return <PeriodContext.Provider value={value}>{children}</PeriodContext.Provider>;
}

/** The area's period; a page drawn outside the area keeps one of its own. */
export function useOrganizationPeriod(): PeriodState {
  const shared = useContext(PeriodContext);
  const [period, setPeriod] = useState<OrganizationPeriod>('7d');
  return shared ?? { period, setPeriod };
}

const SEGMENT = 'rounded px-2.5 py-1 text-[12px] font-medium transition';
const SEGMENT_ON = 'bg-white text-gray-900 shadow-sm';
const SEGMENT_OFF = 'text-gray-500 hover:text-gray-900';

/**
 * 1d, 7d or 30d, with the UTC days it covers beside it. With `onAll`, a
 * fourth choice shows everything, and `value` is then `null`.
 */
export function PeriodControl({
  value,
  onChange,
  onAll,
}: {
  value: OrganizationPeriod | null;
  onChange: (period: OrganizationPeriod) => void;
  onAll?: () => void;
}) {
  const t = useT();
  const fmt = useFormatter();
  return (
    <div className="flex items-center gap-2">
      <div
        role="group"
        aria-label={t('organization.period.label', 'Period')}
        className="inline-flex rounded-md border border-gray-200 bg-gray-50 p-0.5"
      >
        {onAll ? (
          <button
            type="button"
            aria-pressed={value === null}
            onClick={onAll}
            className={`${SEGMENT} ${value === null ? SEGMENT_ON : SEGMENT_OFF}`}
          >
            {t('organization.period.all', 'All')}
          </button>
        ) : null}
        {PERIODS.map((period) => (
          <button
            key={period}
            type="button"
            aria-pressed={value === period}
            aria-label={periodLabel(period, t)}
            onClick={() => onChange(period)}
            className={`${SEGMENT} ${value === period ? SEGMENT_ON : SEGMENT_OFF}`}
          >
            {period}
          </button>
        ))}
      </div>
      {value ? (
        <span className="whitespace-nowrap text-[12px] text-gray-500">
          {periodRange(value, fmt)}
        </span>
      ) : null}
    </div>
  );
}

/* -- one-line pieces --------------------------------------------------------- */

const METER_FILL = { ok: 'bg-blue-500', near: 'bg-amber-500', over: 'bg-red-500' } as const;

/**
 * The bar of a limit: how much of it the month's spend has used. The track is
 * always drawn at its full length, so the bars of a list are one length and
 * only the filled part differs.
 */
export function LimitBar({ spent, limit }: { spent: number | null; limit: number | null }) {
  const use = limitUse(spent, limit);
  if (!use) return null;
  // Nothing spent draws no fill; anything spent draws enough to be seen, also
  // an amount that rounds to 0%.
  const filled = (spent ?? 0) > 0 ? Math.max(3, Math.min(100, use.percent)) : 0;
  return (
    <span
      aria-hidden="true"
      className="block h-1.5 w-full overflow-hidden rounded-full bg-gray-300"
    >
      <span
        className={`block h-full rounded-full ${METER_FILL[use.tone]}`}
        style={{ width: `${filled}%` }}
      />
    </span>
  );
}

/**
 * A member's month against its limit: a bar, then "59% of $130", or the words
 * for no limit. `limitText` is the limit already formatted in its currency.
 *
 * The bar and the figure have fixed places, so they line up down a list.
 * `source` says which limit it is. A limit that the organization did not set
 * for this member has a small word under the figure: the organization's
 * default, or the quota of the platform administrator. The word hangs below
 * the line and moves nothing, so a row with it and a row without it look
 * alike but for that word.
 */
export function LimitMeter({
  spent,
  limit,
  limitText,
  source = 'member',
}: {
  spent: number | null;
  limit: number | null;
  limitText: string;
  source?: MemberLimitSource;
}) {
  const t = useT();
  const tag =
    source === 'default'
      ? {
          word: t('organization.limit.default_tag', 'default'),
          title: t('organization.member.limit_default', 'Default limit of the organization'),
        }
      : source === 'platform'
        ? {
            word: t('organization.limit.platform_tag', 'platform'),
            title: t('organization.member.limit_platform', "Platform administrator's limit"),
          }
        : null;
  const use = limitUse(spent, limit);
  if (!use) {
    return (
      <span className="text-[12px] text-gray-400">{t('organization.limit.none', 'No limit')}</span>
    );
  }
  const tone =
    use.tone === 'over' ? 'text-red-600' : use.tone === 'near' ? 'text-amber-600' : 'text-gray-500';
  return (
    <span className="relative inline-flex items-center justify-end gap-2.5">
      <span className="w-16 shrink-0">
        <LimitBar spent={spent} limit={limit} />
      </span>
      <span
        className={`min-w-[7.5rem] whitespace-nowrap text-right text-[12px] tabular-nums ${tone}`}
      >
        {fill(t('organization.limit.percent_of', '{percent}% of {limit}'), {
          percent: String(use.percent),
          limit: limitText,
        })}
      </span>
      {tag ? (
        <span
          title={tag.title}
          className="absolute right-0 top-full mt-px text-[11px] leading-none text-gray-400"
        >
          {tag.word}
        </span>
      ) : null}
    </span>
  );
}

/** One figure with its label, and a line under it when there is something to add. */
export function StatCard({
  label,
  value,
  aside,
  note,
  children,
}: {
  label: string;
  value: ReactNode;
  /** A quieter value beside the figure: "/ 24", "/ $1,000". */
  aside?: ReactNode;
  note?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className={`${PANEL} min-w-0 p-4`}>
      <p className={FIELD_LABEL}>{label}</p>
      <p className="mt-1 flex flex-wrap items-baseline gap-x-1.5 text-[22px] font-semibold leading-tight tracking-tight text-gray-900 tabular-nums">
        {value}
        {aside ? <span className="text-[13px] font-normal text-gray-500">{aside}</span> : null}
      </p>
      {note ? <p className="mt-1 text-[12px] text-gray-500">{note}</p> : null}
      {children ? <div className="mt-2">{children}</div> : null}
    </div>
  );
}

/** A change against the period before: "−6% vs previous 7 days", colored by its direction. */
export function ChangeNote({ percent, against }: { percent: number | null; against: string }) {
  if (percent === null) return null;
  const sign = percent > 0 ? '+' : percent < 0 ? '−' : '';
  return (
    <span>
      <span className="font-medium tabular-nums text-gray-700">
        {sign}
        {Math.abs(percent)}%
      </span>{' '}
      {against}
    </span>
  );
}

/* -- a card that shows its figures as a chart or as a table ------------------ */

export type FigureView = 'chart' | 'table';

/** A titled card with a Chart / Table switch; the caller draws the chosen view. */
export function FigureCard({
  title,
  note,
  children,
}: {
  title: string;
  note?: string;
  children: (view: FigureView) => ReactNode;
}) {
  const t = useT();
  const [view, setView] = useState<FigureView>('chart');
  const views: ReadonlyArray<[FigureView, string]> = [
    ['chart', t('organization.figure.chart', 'Chart')],
    ['table', t('organization.figure.table', 'Table')],
  ];
  return (
    <section className={`${PANEL} min-w-0 p-4`} aria-label={title}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="text-[13px] font-semibold text-gray-900">{title}</h3>
          {note ? <p className="mt-0.5 text-[12px] text-gray-500">{note}</p> : null}
        </div>
        <div
          role="group"
          aria-label={fill(t('organization.figure.view_of', 'View of {title}'), { title })}
          className="inline-flex rounded-md border border-gray-200 bg-gray-50 p-0.5"
        >
          {views.map(([key, label]) => (
            <button
              key={key}
              type="button"
              aria-pressed={view === key}
              onClick={() => setView(key)}
              className={`${SEGMENT} ${view === key ? SEGMENT_ON : SEGMENT_OFF}`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      <div className="mt-3">{children(view)}</div>
    </section>
  );
}

/* -- paging ------------------------------------------------------------------ */

/**
 * The position in a paged list: a page number, or the cursors of the pages
 * before. It belongs to the filters it was reached with.
 *
 * With other filters the list starts again from `first`, in the same render,
 * and the old position is dropped, not only hidden: a reader who puts the
 * earlier filters back gets their first page, not the page that was open.
 */
export function useListPosition<T>(filters: string, first: T): [T, (next: T) => void] {
  const [held, setHeld] = useState({ filters, position: first });
  const current = held.filters === filters;
  // Set while rendering: React renders again at once, before anything is drawn.
  if (!current) setHeld({ filters, position: first });
  const move = useCallback((next: T) => setHeld({ filters, position: next }), [filters]);
  return [current ? held.position : first, move];
}

/**
 * Keeps a page inside its list. When the list got shorter than the page it is
 * on, which a removal or the end of a period can do, the page goes to the
 * last one that has rows. Without this, the reader is on an empty page with
 * no way back. `total` is undefined until the list is read.
 */
export function useLastPage(
  page: number,
  setPage: (page: number) => void,
  total: number | undefined,
  pageSize: number,
): void {
  useEffect(() => {
    if (total !== undefined && page > 0 && page * pageSize >= total) {
      setPage(Math.max(0, Math.ceil(total / pageSize) - 1));
    }
  }, [page, pageSize, setPage, total]);
}

/** "Showing 1–50 of 132", with Previous and Next. */
export function Pager({
  page,
  pageSize,
  total,
  count,
  onPageChange,
}: {
  /** Zero-based. */
  page: number;
  pageSize: number;
  total: number;
  /** Rows on this page. */
  count: number;
  onPageChange: (page: number) => void;
}) {
  const t = useT();
  if (total <= pageSize && page === 0) return null;
  const first = count > 0 ? page * pageSize + 1 : 0;
  const last = count > 0 ? page * pageSize + count : 0;
  const pages = Math.max(1, Math.ceil(total / pageSize));
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 px-1 py-2 text-[12px] text-gray-500">
      <span>
        {fill(t('organization.pager.showing', 'Showing {first}–{last} of {total}'), {
          first: String(first),
          last: String(last),
          total: String(total),
        })}
      </span>
      <div className="flex items-center gap-2">
        <button
          type="button"
          className={BUTTON_SECONDARY}
          disabled={page <= 0}
          onClick={() => onPageChange(page - 1)}
        >
          {t('organization.pager.previous', 'Previous')}
        </button>
        <button
          type="button"
          className={BUTTON_SECONDARY}
          disabled={page >= pages - 1}
          onClick={() => onPageChange(page + 1)}
        >
          {t('organization.pager.next', 'Next')}
        </button>
      </div>
    </div>
  );
}
