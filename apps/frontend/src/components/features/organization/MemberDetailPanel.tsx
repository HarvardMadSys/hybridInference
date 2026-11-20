'use client';

import { useT } from '@/components/providers/useT';
import { AdminLoading, AdminNotice } from '@/components/features/admin/AdminPrimitives';
import {
  BUTTON_DANGER,
  BUTTON_SECONDARY,
  NOTICE_ACTION,
  PANEL,
} from '@/components/features/admin/adminStyles';
import type { OrganizationMember, OrganizationPeriod } from '@/lib/api/organization';
import { useOrganizationMember } from '@/lib/hooks';
import { useMoney } from '@/lib/hooks/useMoney';
import { useFormatter } from '@/lib/i18n/useFormatter';
import { getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { moneyAmount } from '@/lib/utils/money';
import { RequestsTable, SeriesFigure, SpendByModel, seriesCurrency } from './figures';
import type { MemberDialog } from './MemberDialogs';
import { LimitBar, StatCard, StatusPill } from './parts';
import { compactNumber, memberStatus, periodLabel, relativeTime } from './text';

/** What an organization administrator may do to a member, from the member's row. */
export function memberActions(member: OrganizationMember): {
  limit: boolean;
  remove: boolean;
  restore: boolean;
} {
  // An administrator's limit and removal are the platform administrator's.
  if (member.role === 'admin') return { limit: false, remove: false, restore: false };
  return {
    limit: true,
    remove: member.status === 'active',
    // Only a removal by the organization is the organization's to undo.
    restore: member.removed,
  };
}

/**
 * One member, as its panel shows it: its status, when it joined, what an
 * administrator may do to it, its figures for the period, its spend for each
 * of the last 30 days, the models it used and its last requests.
 */
export function MemberDetailPanel({
  member,
  period,
  onAction,
}: {
  /** The member as its row had it; shown until the member's own details arrive. */
  member: OrganizationMember;
  period: OrganizationPeriod;
  onAction: (dialog: MemberDialog) => void;
}) {
  const t = useT();
  const money = useMoney();
  const fmt = useFormatter();
  const { data, error, isLoading, refetch } = useOrganizationMember(member.user_id, period);
  const current = data?.member ?? member;
  const actions = memberActions(current);
  const hasAction = actions.limit || actions.remove || actions.restore;
  const limit = moneyAmount(current, 'monthly_limit');
  const spendCurrency = seriesCurrency(data?.days ?? [], current);
  const inPeriod = periodLabel(period, t);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <StatusPill tone={memberStatus(current, t)} />
        <p className="text-[12px] text-gray-500">
          {fill(t('organization.member.joined', 'Joined {date} · Last sign-in {when}'), {
            date: fmt.date(current.joined_at, { year: 'numeric', month: 'short', day: 'numeric' }),
            when: relativeTime(current.last_login_at, t, fmt),
          })}
        </p>
      </div>

      {hasAction ? (
        <div className="flex flex-wrap items-center gap-2">
          {actions.limit ? (
            <button
              type="button"
              className={BUTTON_SECONDARY}
              onClick={() => onAction({ mode: 'limit', member: current })}
            >
              {t('organization.member.set_limit', 'Set monthly limit')}
            </button>
          ) : null}
          {actions.remove ? (
            <button
              type="button"
              className={BUTTON_DANGER}
              onClick={() => onAction({ mode: 'remove', member: current })}
            >
              {t('organization.member.remove', 'Remove from organization')}
            </button>
          ) : null}
          {actions.restore ? (
            <button
              type="button"
              className={BUTTON_SECONDARY}
              onClick={() => onAction({ mode: 'restore', member: current })}
            >
              {t('organization.member.restore', 'Restore member')}
            </button>
          ) : null}
        </div>
      ) : null}

      {current.role === 'admin' ? (
        <p className="text-[12px] text-gray-500">
          {t(
            'organization.member.admin_note',
            'The platform administrator sets the limit of an organization administrator and removes one.',
          )}
        </p>
      ) : current.status === 'suspended' && !current.removed ? (
        <p className="text-[12px] text-gray-500">
          {t(
            'organization.member.suspended_note',
            'The platform administrator suspended this account, and only the platform administrator can make it active again.',
          )}
        </p>
      ) : null}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard
          label={t('organization.overview.spend', 'Spend')}
          value={money.value(current, 'cost', { maximumFractionDigits: 2 })}
          note={inPeriod}
        />
        <StatCard
          label={t('organization.overview.requests', 'Requests')}
          value={fmt.number(current.requests)}
          note={inPeriod}
        />
        <StatCard
          label={t('organization.overview.tokens', 'Tokens')}
          value={compactNumber(current.total_tokens, fmt)}
          note={inPeriod}
        />
        <StatCard
          label={t('organization.member.this_month', 'This month')}
          value={money.value(current, 'spent_month', { maximumFractionDigits: 2 })}
          aside={
            limit === null
              ? undefined
              : `/ ${money.value(current, 'monthly_limit', { maximumFractionDigits: 2 })}`
          }
          note={
            current.monthly_limit_source === 'member'
              ? t('organization.member.limit_own', 'Monthly limit of this member')
              : current.monthly_limit_source === 'default'
                ? t('organization.member.limit_default', 'Default limit of the organization')
                : t('organization.member.limit_none', 'No monthly limit')
          }
        >
          <LimitBar spent={moneyAmount(current, 'spent_month')} limit={limit} />
        </StatCard>
      </div>

      {isLoading ? (
        <AdminLoading size="sm" label={t('common.loading', 'Loading...')} />
      ) : !data ? (
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
      ) : (
        <>
          <SeriesFigure
            title={fill(t('organization.overview.spend_per_day', 'Spend per day ({currency})'), {
              currency: spendCurrency,
            })}
            note={t('organization.member.last_30_days', 'Last 30 days')}
            kind="bar"
            series={data.days}
            hourly={false}
            bucketHeader={t('organization.overview.day', 'Day (UTC)')}
            valueHeader={t('organization.overview.spend', 'Spend')}
            valueOf={(point) => moneyAmount(point, 'cost') ?? 0}
            formatValue={(value) =>
              money.format(value, { maximumFractionDigits: 2 }, spendCurrency)
            }
          />
          <section
            className={`${PANEL} p-4`}
            aria-label={t('organization.overview.by_model', 'Spend by model')}
          >
            <h3 className="text-[13px] font-semibold text-gray-900">
              {t('organization.overview.by_model', 'Spend by model')}
            </h3>
            <p className="mb-3 mt-0.5 text-[12px] text-gray-500">{inPeriod}</p>
            <SpendByModel models={data.models} limit={6} />
          </section>
          <section aria-label={t('organization.member.recent', 'Recent requests')}>
            <h3 className="mb-2 text-[13px] font-semibold text-gray-900">
              {t('organization.member.recent', 'Recent requests')}
            </h3>
            <RequestsTable
              requests={data.requests}
              showMember={false}
              empty={t('organization.member.no_requests', 'No requests in this period.')}
            />
          </section>
        </>
      )}
    </div>
  );
}
