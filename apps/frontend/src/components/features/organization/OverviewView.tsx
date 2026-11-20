'use client';

import Link from 'next/link';
import { useT } from '@/components/providers/useT';
import { AdminLoading, AdminNotice } from '@/components/features/admin/AdminPrimitives';
import { NOTICE_ACTION, PANEL } from '@/components/features/admin/adminStyles';
import { useOrganizationOverview } from '@/lib/hooks';
import { useMoney } from '@/lib/hooks/useMoney';
import { useFormatter } from '@/lib/i18n/useFormatter';
import { getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { moneyAmount } from '@/lib/utils/money';
import { SeriesFigure, SpendByModel, seriesCurrency } from './figures';
import { organizationHref } from './OrganizationTabNav';
import { ChangeNote, LimitBar, PeriodControl, StatCard, useOrganizationPeriod } from './parts';
import { changePercent, compactNumber, previousPeriodLabel } from './text';

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
  const { data, error, isLoading, isPlaceholderData, refetch } = useOrganizationOverview(period);

  const toolbar = (
    <div className="flex flex-wrap items-center gap-2">
      <PeriodControl value={period} onChange={setPeriod} />
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
          <Link
            href={`${organizationHref('usage')}?view=models`}
            prefetch={false}
            className="text-[12px] font-medium text-gray-600 underline decoration-gray-300 underline-offset-4 transition hover:text-gray-900"
          >
            {t('organization.overview.view_by_model', 'View by model')}
          </Link>
        </div>
        <SpendByModel models={data.models} />
      </section>
    </div>
  );
}
