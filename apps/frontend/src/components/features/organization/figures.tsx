'use client';

import dynamic from 'next/dynamic';
import { useT } from '@/components/providers/useT';
import { AdminEmptyState } from '@/components/features/admin/AdminPrimitives';
import { PANEL, TABLE_CELL, TABLE_HEAD_CELL } from '@/components/features/admin/adminStyles';
import type { ModelUsage, OrganizationRequest, UsagePoint } from '@/lib/api/organization';
import { useMoney } from '@/lib/hooks/useMoney';
import { useFormatter, type Formatter } from '@/lib/i18n/useFormatter';
import { moneyAmount, moneyCurrency, type Currency } from '@/lib/utils/money';
import { StatusPill } from '@/components/features/console-shared/StatusPill';
import { FigureCard } from './parts';
import { requestStatus } from './text';

// The chart carries the recharts bundle; a page shows its figures first.
const SeriesChart = dynamic(() => import('./OrganizationCharts'), {
  ssr: false,
  loading: () => <div className="h-[220px] animate-pulse rounded bg-gray-50" />,
});

const NUM_HEAD = `${TABLE_HEAD_CELL} text-right`;
const NUM_CELL = `${TABLE_CELL} whitespace-nowrap text-right tabular-nums`;

/** A point's place on the axis: its UTC day, or its UTC hour for a day of hours. */
export function bucketLabel(point: UsagePoint, hourly: boolean, fmt: Formatter): string {
  return hourly
    ? fmt.dateTime(point.bucket, {
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
        timeZone: 'UTC',
      })
    : fmt.date(point.bucket, { month: 'short', day: 'numeric', timeZone: 'UTC' });
}

/**
 * The currency of the spend in a series: the one the gateway sent with its
 * points, or with `holder`, what the series came in, when it has no point.
 *
 * Never the currency of the page's own configuration. A page that stayed open
 * while the ledger changed still holds the old one, and would write the new
 * amounts under the old sign.
 */
export function seriesCurrency(series: UsagePoint[], holder: object): Currency {
  return moneyCurrency(series[0] ?? holder);
}

/**
 * How an amount is written on the axis of a chart: short, 1.35K for 1,350.00,
 * because the axis has room for a few characters only. The tooltip and the
 * table have the amount in full.
 */
export const AXIS_AMOUNT: Intl.NumberFormatOptions = {
  notation: 'compact',
  minimumFractionDigits: 0,
  maximumFractionDigits: 2,
};

/**
 * One figure of a usage series over a period, as a chart or as a table of the
 * same numbers.
 */
export function SeriesFigure({
  title,
  note,
  kind,
  series,
  hourly,
  bucketHeader,
  valueHeader,
  valueOf,
  formatValue,
  formatTick,
}: {
  title: string;
  note?: string;
  kind: 'line' | 'bar';
  series: UsagePoint[];
  hourly: boolean;
  bucketHeader: string;
  valueHeader: string;
  valueOf: (point: UsagePoint) => number;
  formatValue: (value: number) => string;
  /** A value on the axis of the chart, when the full text is too long for it. */
  formatTick?: (value: number) => string;
}) {
  const t = useT();
  const fmt = useFormatter();
  const points = series.map((point) => ({
    label: bucketLabel(point, hourly, fmt),
    value: valueOf(point),
  }));
  const quiet = points.every((point) => point.value === 0);
  return (
    <FigureCard title={title} note={note}>
      {(view) =>
        quiet ? (
          <AdminEmptyState
            variant="plain"
            title={t('organization.figure.empty', 'Nothing in this period.')}
          />
        ) : view === 'chart' ? (
          <SeriesChart
            kind={kind}
            points={points}
            name={valueHeader}
            formatValue={formatValue}
            formatTick={formatTick}
          />
        ) : (
          <div className="max-h-[220px] overflow-y-auto">
            <table className="min-w-full">
              <thead>
                <tr className="border-b border-gray-100">
                  <th scope="col" className={TABLE_HEAD_CELL}>
                    {bucketHeader}
                  </th>
                  <th scope="col" className={NUM_HEAD}>
                    {valueHeader}
                  </th>
                </tr>
              </thead>
              <tbody>
                {points.map((point) => (
                  <tr key={point.label} className="border-b border-gray-100 last:border-b-0">
                    <td className={TABLE_CELL}>{point.label}</td>
                    <td className={NUM_CELL}>{formatValue(point.value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      }
    </FigureCard>
  );
}

/** The models of a period, the largest spend first, each with a bar of its share. */
export function SpendByModel({ models, limit = 8 }: { models: ModelUsage[]; limit?: number }) {
  const t = useT();
  const money = useMoney();
  const rows = models
    .map((model) => ({ model, cost: moneyAmount(model, 'cost') ?? 0 }))
    .filter((row) => row.cost > 0)
    .sort((a, b) => b.cost - a.cost)
    .slice(0, limit);
  if (rows.length === 0) {
    return (
      <AdminEmptyState
        variant="plain"
        title={t('organization.figure.empty', 'Nothing in this period.')}
      />
    );
  }
  const largest = rows[0].cost;
  return (
    // One grid for the list, and each row on its columns: the amounts then have
    // one width, so every bar has the same track and the bars are on one scale.
    <ul className="grid grid-cols-[minmax(0,12rem)_minmax(0,1fr)_auto] gap-x-3 gap-y-2 text-[13px]">
      {rows.map(({ model, cost }) => (
        <li key={model.model_id} className="col-span-3 grid grid-cols-subgrid items-center">
          <span className="truncate text-gray-700" title={model.model_id}>
            {model.model_id}
          </span>
          <span aria-hidden="true" className="block h-2 overflow-hidden rounded-full bg-gray-100">
            <span
              className="block h-full rounded-full bg-blue-500"
              style={{ width: `${Math.max(2, (cost / largest) * 100)}%` }}
            />
          </span>
          <span className="whitespace-nowrap text-right tabular-nums text-gray-900">
            {money.value(model, 'cost', { maximumFractionDigits: 2 })}
          </span>
        </li>
      ))}
    </ul>
  );
}

/**
 * Requests, one row each, newest first: who, with which key and model, how it
 * ended and what it cost. The gateway sends no content and no key value, so
 * there is none to show; a key appears by its name.
 */
export function RequestsTable({
  requests,
  showMember = true,
  empty,
}: {
  requests: OrganizationRequest[];
  /** Left out in one member's own details. */
  showMember?: boolean;
  empty: string;
}) {
  const t = useT();
  const money = useMoney();
  const fmt = useFormatter();
  if (requests.length === 0) {
    return <AdminEmptyState variant="plain" title={empty} />;
  }
  return (
    <div className={`${PANEL} overflow-x-auto`}>
      <table className="min-w-full">
        <thead>
          <tr className="border-b border-gray-200 bg-gray-50">
            <th scope="col" className={TABLE_HEAD_CELL}>
              {t('organization.requests.time', 'Time')}
            </th>
            {showMember ? (
              <th scope="col" className={TABLE_HEAD_CELL}>
                {t('organization.requests.member', 'Member')}
              </th>
            ) : null}
            <th scope="col" className={TABLE_HEAD_CELL}>
              {t('organization.requests.project', 'Project')}
            </th>
            <th scope="col" className={TABLE_HEAD_CELL}>
              {t('organization.requests.key', 'API key')}
            </th>
            <th scope="col" className={TABLE_HEAD_CELL}>
              {t('organization.requests.model', 'Model')}
            </th>
            <th scope="col" className={TABLE_HEAD_CELL}>
              {t('organization.requests.status', 'Status')}
            </th>
            <th scope="col" className={NUM_HEAD}>
              {t('organization.requests.input_tokens', 'Input tokens')}
            </th>
            <th scope="col" className={NUM_HEAD}>
              {t('organization.requests.output_tokens', 'Output tokens')}
            </th>
            <th scope="col" className={NUM_HEAD}>
              {t('organization.requests.cost', 'Cost')}
            </th>
          </tr>
        </thead>
        <tbody>
          {requests.map((request) => (
            <tr key={request.id} className="border-b border-gray-100 last:border-b-0">
              <td className={`${TABLE_CELL} whitespace-nowrap text-gray-500`}>
                {fmt.dateTime(request.timestamp, {
                  month: 'short',
                  day: 'numeric',
                  hour: '2-digit',
                  minute: '2-digit',
                })}
              </td>
              {showMember ? (
                <td className={`${TABLE_CELL} max-w-[16rem] truncate`}>
                  {request.email || request.user_name || request.user_id}
                </td>
              ) : null}
              <td className={`${TABLE_CELL} max-w-[10rem] truncate text-gray-500`}>
                {request.project_name || '—'}
              </td>
              <td className={`${TABLE_CELL} max-w-[10rem] truncate text-gray-500`}>
                {request.key_name || '—'}
              </td>
              <td className={`${TABLE_CELL} max-w-[14rem] truncate`} title={request.model_id}>
                {request.model_id}
              </td>
              <td className={TABLE_CELL}>
                <StatusPill tone={requestStatus(request, t)} />
              </td>
              <td className={NUM_CELL}>{fmt.number(request.prompt_tokens)}</td>
              <td className={NUM_CELL}>{fmt.number(request.completion_tokens)}</td>
              <td className={NUM_CELL}>{money.requestCost(request)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
