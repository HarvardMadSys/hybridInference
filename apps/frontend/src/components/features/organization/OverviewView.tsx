'use client';

import Link from 'next/link';
import { useState } from 'react';
import { useT } from '@/components/providers/useT';
import { AdminLoading, AdminNotice } from '@/components/features/admin/AdminPrimitives';
import {
  NOTICE_ACTION,
  PANEL,
  TABLE_CELL,
  TABLE_HEAD_CELL,
} from '@/components/features/admin/adminStyles';
import { ModelTokenTable } from '@/components/features/console-shared/ModelTokenTable';
import type { OrganizationPeriod } from '@/lib/api/organization';
import { useOrganizationOverview } from '@/lib/hooks';
import { useMoney } from '@/lib/hooks/useMoney';
import { useFormatter } from '@/lib/i18n/useFormatter';
import { getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { moneyAmount } from '@/lib/utils/money';
import { AXIS_AMOUNT, SeriesFigure, SpendByModel, seriesCurrency } from './figures';
import { organizationHref } from './OrganizationTabNav';
import { ChangeNote, LimitBar, PeriodControl, StatCard, useOrganizationPeriod } from './parts';
import { changePercent, compactNumber, memberName, previousPeriodLabel } from './text';

type Granularity = 'hour' | 'day';

const SEGMENT = 'rounded px-2.5 py-1 text-[12px] font-medium transition';
const NUM_HEAD = `${TABLE_HEAD_CELL} text-right`;
const NUM_CELL = `${TABLE_CELL} whitespace-nowrap text-right tabular-nums`;
const LINK =
  'text-[12px] font-medium text-gray-600 underline decoration-gray-300 underline-offset-4 transition hover:text-gray-900';

/**
 * The points of the series for a period: what the reader chose, where the
 * period has the choice. A day of hours is the series of `1d`, and `30d` has
 * days only.
 */
export function seriesGranularity(
  period: OrganizationPeriod,
  chosen: Granularity | null,
): Granularity {
  if (period === '30d') return 'day';
  return chosen ?? (period === '1d' ? 'hour' : 'day');
}

/**
 * The organization at a glance: what its members spent and asked for in the
 * period, how many of them were active, and where the month stands against
 * the organization's limit.
 */
export function OverviewView() {
  const t = useT();
  const money = useMoney();
  const fmt = useFormatter();
  const { period, setPeriod } = useOrganizationPeriod();
  // What the reader chose for the series; until then, what the period has by itself.
  const [chosen, setChosen] = useState<Granularity | null>(null);
  const granularity = seriesGranularity(period, chosen);
  const { data, error, isLoading, isPlaceholderData, refetch } = useOrganizationOverview(
    period,
    // The gateway has no hourly series of 30 days, and is not asked for one.
    period === '30d' ? undefined : granularity,
  );

  const granularities: ReadonlyArray<[Granularity, string]> = [
    ['hour', t('organization.overview.by_hour', 'By hour')],
    ['day', t('organization.overview.by_day', 'By day')],
  ];
  const toolbar = (
    <div className="flex flex-wrap items-center gap-2">
      <PeriodControl value={period} onChange={setPeriod} />
      {period === '30d' ? null : (
        <div
          role="group"
          aria-label={t('organization.overview.granularity', 'Points of the series')}
          className="inline-flex rounded-md border border-gray-200 bg-gray-50 p-0.5"
        >
          {granularities.map(([key, label]) => (
            <button
              key={key}
              type="button"
              aria-pressed={granularity === key}
              onClick={() => setChosen(key)}
              className={`${SEGMENT} ${
                granularity === key
                  ? 'bg-white text-gray-900 shadow-sm'
                  : 'text-gray-500 hover:text-gray-900'
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      )}
    </div>
  );

  if (isLoading) {
    return (
      <div className="space-y-4">
        {toolbar}
        <AdminLoading label={t('common.loading', 'Loading...')} />
      </div>
    );
  }
  if (!data) {
    return (
      <div className="space-y-4">
        {toolbar}
        <AdminNotice
          role="alert"
          action={
            <button type="button" className={NOTICE_ACTION} onClick={() => void refetch()}>
              {t('organization.retry', 'Retry')}
            </button>
          }
        >
          {getErrorMessage(error)}
        </AdminNotice>
      </div>
    );
  }

  const hourly = data.bucket === 'hour';
  const spendCurrency = seriesCurrency(data.series, data);
  const spent = moneyAmount(data, 'cost') ?? 0;
  const against = previousPeriodLabel(period, t);
  const monthSpent = moneyAmount(data, 'spent_month');
  const monthLimit = moneyAmount(data, 'monthly_limit');
  const bucketHeader = hourly
    ? t('organization.overview.hour', 'Hour (UTC)')
    : t('organization.overview.day', 'Day (UTC)');
  const topMembers = data.top_members ?? [];

  return (
    <div className={`space-y-4 transition-opacity ${isPlaceholderData ? 'opacity-60' : ''}`}>
      {toolbar}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <StatCard
          label={t('organization.overview.spend', 'Spend')}
          value={money.value(data, 'cost', { maximumFractionDigits: 2 })}
          note={
            <ChangeNote
              percent={changePercent(spent, moneyAmount(data.previous, 'cost'))}
              against={against}
            />
          }
        />
        <StatCard
          label={t('organization.overview.requests', 'Requests')}
          value={fmt.number(data.requests)}
          note={
            <ChangeNote
              percent={changePercent(data.requests, data.previous?.requests)}
              against={against}
            />
          }
        />
        <StatCard
          label={t('organization.overview.tokens', 'Tokens')}
          value={compactNumber(data.total_tokens, fmt)}
        />
        <StatCard
          label={t('organization.overview.active_members', 'Members with requests')}
          value={fmt.number(data.members_with_requests)}
          aside={`/ ${fmt.number(data.member_count)}`}
        />
        <StatCard
          label={t('organization.overview.limit', 'Organization limit, this month')}
          value={money.value(data, 'spent_month', { maximumFractionDigits: 2 })}
          aside={
            monthLimit === null
              ? undefined
              : `/ ${money.value(data, 'monthly_limit', { maximumFractionDigits: 2 })}`
          }
          note={
            monthLimit === null
              ? t('organization.overview.no_limit', 'The organization has no monthly limit.')
              : undefined
          }
        >
          <LimitBar spent={monthSpent} limit={monthLimit} />
        </StatCard>
      </div>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <SeriesFigure
          title={
            hourly
              ? t('organization.overview.active_per_hour', 'Members with requests per hour')
              : t('organization.overview.active_per_day', 'Members with requests per day')
          }
          kind="line"
          series={data.series}
          hourly={hourly}
          bucketHeader={bucketHeader}
          valueHeader={t('organization.overview.members', 'Members')}
          valueOf={(point) => point.members_with_requests}
          formatValue={(value) => fmt.number(value)}
        />
        <SeriesFigure
          title={fill(
            hourly
              ? t('organization.overview.spend_per_hour', 'Spend per hour ({currency})')
              : t('organization.overview.spend_per_day', 'Spend per day ({currency})'),
            { currency: spendCurrency },
          )}
          kind="bar"
          series={data.series}
          hourly={hourly}
          bucketHeader={bucketHeader}
          valueHeader={t('organization.overview.spend', 'Spend')}
          valueOf={(point) => moneyAmount(point, 'cost') ?? 0}
          formatValue={(value) => money.format(value, { maximumFractionDigits: 2 }, spendCurrency)}
          formatTick={(value) => money.format(value, AXIS_AMOUNT, spendCurrency)}
        />
      </div>

      <section
        className={`${PANEL} p-4`}
        aria-label={t('organization.overview.by_model', 'Spend by model')}
      >
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-[13px] font-semibold text-gray-900">
            {t('organization.overview.by_model', 'Spend by model')}
          </h3>
          <Link href={`${organizationHref('usage')}?view=models`} prefetch={false} className={LINK}>
            {t('organization.overview.view_by_model', 'View by model')}
          </Link>
        </div>
        <SpendByModel models={data.models} />
      </section>

      {data.models.length > 0 ? (
        <ModelTokenTable
          title={t('organization.overview.tokens_by_model', 'Tokens by model')}
          rows={data.models.map((model) => ({
            model_id: model.model_id,
            input_tokens: model.prompt_tokens,
            output_tokens: model.completion_tokens,
            cached_tokens: model.cached_tokens ?? 0,
            request_count: model.requests,
          }))}
          formatCount={(value) => fmt.number(value)}
          columns={['input', 'output', 'cached', 'requests']}
          // As the tables beside it on this page, not as the Admin console's.
          look={{
            frame: `${PANEL} overflow-x-auto`,
            title: 'px-4 pb-2 pt-4 text-[13px] font-semibold text-gray-900',
            table: 'min-w-full',
            head: '',
            headRow: 'border-y border-gray-200 bg-gray-50',
            modelHead: TABLE_HEAD_CELL,
            numberHead: NUM_HEAD,
            row: 'border-b border-gray-100 last:border-b-0',
            modelCell: TABLE_CELL,
            numberCell: NUM_CELL,
          }}
        />
      ) : null}

      <section
        className={`${PANEL} overflow-x-auto`}
        aria-label={t('organization.overview.top_members', 'Top members')}
      >
        <div className="flex flex-wrap items-center justify-between gap-2 px-4 pb-2 pt-4">
          <h3 className="text-[13px] font-semibold text-gray-900">
            {t('organization.overview.top_members', 'Top members')}
          </h3>
          <Link
            href={`${organizationHref('usage')}?view=members`}
            prefetch={false}
            className={LINK}
          >
            {t('organization.overview.view_by_member', 'View by member')}
          </Link>
        </div>
        {topMembers.length === 0 ? (
          <p className="px-4 pb-4 text-[13px] text-gray-400">
            {t('organization.figure.empty', 'Nothing in this period.')}
          </p>
        ) : (
          <table className="min-w-full">
            <thead>
              <tr className="border-y border-gray-200 bg-gray-50">
                <th scope="col" className={TABLE_HEAD_CELL}>
                  {t('organization.members.member', 'Member')}
                </th>
                <th scope="col" className={NUM_HEAD}>
                  {t('organization.overview.requests', 'Requests')}
                </th>
                <th scope="col" className={NUM_HEAD}>
                  {t('organization.overview.tokens', 'Tokens')}
                </th>
                <th scope="col" className={NUM_HEAD}>
                  {t('organization.overview.spend', 'Spend')}
                </th>
              </tr>
            </thead>
            <tbody>
              {topMembers.map((member) => (
                <tr key={member.user_id} className="border-b border-gray-100 last:border-b-0">
                  <td className={`${TABLE_CELL} max-w-[20rem] truncate`}>{memberName(member)}</td>
                  <td className={NUM_CELL}>{fmt.number(member.requests)}</td>
                  <td className={NUM_CELL}>{compactNumber(member.total_tokens, fmt)}</td>
                  <td className={`${NUM_CELL} text-gray-900`}>
                    {money.value(member, 'cost', { maximumFractionDigits: 2 })}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
