'use client';

import type { Translate } from '@/components/providers/useT';
import { useT } from '@/components/providers/useT';
import { AdminLoading, AdminNotice } from '@/components/features/admin/AdminPrimitives';
import {
  BUTTON_DANGER,
  BUTTON_SECONDARY,
  NOTICE_ACTION,
  PANEL,
  ROW_ACTION_DANGER,
} from '@/components/features/admin/adminStyles';
import {
  ProjectsAndKeys,
  type KeyEntry,
} from '@/components/features/console-shared/ProjectsAndKeys';
import { StatusPill } from '@/components/features/console-shared/StatusPill';
import type {
  MemberAccount,
  MemberDetail,
  MemberKey,
  OrganizationMember,
  OrganizationPeriod,
} from '@/lib/api/organization';
import { useOrganizationMember } from '@/lib/hooks';
import { useMoney } from '@/lib/hooks/useMoney';
import { useFormatter } from '@/lib/i18n/useFormatter';
import { getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { moneyAmount } from '@/lib/utils/money';
import { AXIS_AMOUNT, RequestsTable, SeriesFigure, SpendByModel, seriesCurrency } from './figures';
import type { MemberDialog } from './MemberDialogs';
import { LimitBar, StatCard } from './parts';
import { compactNumber, memberStatus, periodLabel, relativeTime } from './text';

/** What an organization administrator may do to a member, from the member's row. */
export function memberActions(member: OrganizationMember): {
  limit: boolean;
  /** Turn a model off for the member, and revoke one of its API keys. */
  access: boolean;
  remove: boolean;
  restore: boolean;
} {
  // The limit, the models, the keys and the removal of an administrator are
  // the platform administrator's: the gateway refuses each. It marks such a
  // member, an administrator of the platform too, which the role here does
  // not show.
  if (member.protected ?? member.role === 'admin') {
    return { limit: false, access: false, remove: false, restore: false };
  }
  return {
    limit: true,
    access: true,
    remove: member.status === 'active',
    // Only a removal by the organization is the organization's to undo.
    restore: member.removed,
  };
}

/** What the account has, in three short phrases. */
function accountFacts(account: MemberAccount, t: Translate): string[] {
  return [
    account.has_key
      ? t('organization.member.has_key', 'Has an active API key')
      : t('organization.member.no_key', 'No active API key'),
    account.two_factor_enabled
      ? t('organization.member.two_factor_on', 'Two-factor on')
      : t('organization.member.two_factor_off', 'Two-factor off'),
    account.email_verified
      ? t('organization.member.email_verified', 'Email verified')
      : t('organization.member.email_unverified', 'Email not verified'),
  ];
}

/** The word of a key that is not active. */
function keyStatus(status: string, t: Translate): string | undefined {
  if (status === 'active') return undefined;
  return status === 'revoked' ? t('organization.member.key_revoked', 'revoked') : status;
}

/**
 * The projects of a member and the keys in each, by name: what each spent in
 * this month, and when a key was last used. An active key has a control to
 * revoke it, for a member the organization may act on.
 */
function MemberProjects({
  detail,
  member,
  canRevoke,
  onAction,
}: {
  detail: MemberDetail;
  member: OrganizationMember;
  canRevoke: boolean;
  onAction: (dialog: MemberDialog) => void;
}) {
  const t = useT();
  const money = useMoney();
  const fmt = useFormatter();
  const projects = detail.projects ?? [];
  const keys = detail.keys ?? [];
  const thisMonth = (source: object) =>
    fill(t('organization.member.spent_this_month', '{amount} this month'), {
      amount: money.value(source, 'spent_month', { maximumFractionDigits: 2 }),
    });
  const entry = (key: MemberKey): KeyEntry => ({
    id: String(key.id),
    name: key.name ?? null,
    statusNote: keyStatus(key.status, t),
    figures: `${thisMonth(key)} · ${
      key.last_used_at
        ? fill(t('organization.member.key_last_used', 'last used {when}'), {
            when: relativeTime(key.last_used_at, t, fmt),
          })
        : t('organization.member.key_never_used', 'never used')
    }`,
    action:
      canRevoke && key.status === 'active' ? (
        <button
          type="button"
          className={ROW_ACTION_DANGER}
          aria-label={fill(t('organization.member.revoke_key_named', 'Revoke {key}'), {
            key: key.name ?? t('console.projects.unnamed_key', 'Unnamed key'),
          })}
          onClick={() => onAction({ mode: 'revoke-key', member, apiKey: key })}
        >
          {t('organization.member.revoke_key', 'Revoke')}
        </button>
      ) : undefined,
  });
  const projectIds = new Set(projects.map((project) => project.id));
  return (
    <ProjectsAndKeys
      projects={projects.map((project) => ({
        id: project.id,
        name: project.name,
        isDefault: project.is_default,
        figures: thisMonth(project),
        keys: keys.filter((key) => key.project_id === project.id).map(entry),
      }))}
      archivedKeys={keys.filter((key) => !projectIds.has(key.project_id ?? '')).map(entry)}
    />
  );
}

/**
 * The models that are off for a member: those of the organization, which the
 * "Models" action changes, and those of the platform administrator, which the
 * organization reads.
 */
function MemberModelsOff({ detail }: { detail: MemberDetail }) {
  const t = useT();
  const own = detail.disabled_models ?? [];
  const platform = detail.platform_disabled_models ?? [];
  return (
    <section
      className={`${PANEL} space-y-1.5 p-4`}
      aria-label={t('organization.member.models_off', 'Models turned off')}
    >
      <h3 className="text-[13px] font-semibold text-gray-900">
        {t('organization.member.models_off', 'Models turned off')}
      </h3>
      {own.length === 0 && platform.length === 0 ? (
        <p className="text-[12px] text-gray-400">
          {t('organization.member.models_off_none', 'No model is turned off for this member.')}
        </p>
      ) : null}
      {own.length > 0 ? (
        <p className="text-[12px] text-gray-700">
          <span className="text-gray-500">
            {t('organization.member.models_off_own', 'By the organization:')}
          </span>{' '}
          {own.join(', ')}
        </p>
      ) : null}
      {platform.length > 0 ? (
        <p className="text-[12px] text-gray-700">
          <span className="text-gray-500">
            {t('organization.member.models_off_platform', 'By the platform administrator:')}
          </span>{' '}
          {platform.join(', ')}
        </p>
      ) : null}
    </section>
  );
}

/**
 * One member, as its panel shows it: its status, when it joined, what its
 * account has, what an administrator may do to it, its spend, its spend for
 * each of the last 30 days, the models it used, its projects and keys, the
 * models that are off for it, and its last requests.
 */
export function MemberDetailPanel({
  member,
  period,
  onAction,
}: {
  /** The member as its row had it; shown until the member's own details arrive. */
  member: OrganizationMember;
  /** The period of the requests, the tokens and the models; the spend figures do not follow it. */
  period: OrganizationPeriod;
  onAction: (dialog: MemberDialog) => void;
}) {
  const t = useT();
  const money = useMoney();
  const fmt = useFormatter();
  const { data, error, isLoading, refetch } = useOrganizationMember(member.user_id, period);
  const current = data?.member ?? member;
  const actions = memberActions(current);
  const hasAction = actions.limit || actions.access || actions.remove || actions.restore;
  const limit = moneyAmount(current, 'monthly_limit');
  const spendCurrency = seriesCurrency(data?.days ?? [], current);
  const inPeriod = periodLabel(period, t);
  const amount = (field: string) => money.value(current, field, { maximumFractionDigits: 2 });

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
      {data?.account ? (
        <p className="text-[12px] text-gray-500">{accountFacts(data.account, t).join(' · ')}</p>
      ) : null}

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
          {actions.access ? (
            <button
              type="button"
              className={BUTTON_SECONDARY}
              onClick={() => onAction({ mode: 'models', member: current })}
            >
              {t('organization.member.set_models', 'Models')}
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
      ) : current.protected ? (
        <p className="text-[12px] text-gray-500">
          {t(
            'organization.member.protected_note',
            'The platform administrator sets the limit of this account and removes it.',
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

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
        <StatCard label={t('organization.members.today', 'Today')} value={amount('spent_today')} />
        <StatCard
          label={t('organization.member.last_7_days', 'Last 7 days')}
          value={amount('spent_7d')}
        />
        <StatCard
          label={t('organization.member.this_month', 'This month')}
          value={amount('spent_month')}
          aside={limit === null ? undefined : `/ ${amount('monthly_limit')}`}
          note={
            current.monthly_limit_source === 'member'
              ? t('organization.member.limit_own', 'Monthly limit of this member')
              : current.monthly_limit_source === 'default'
                ? t('organization.member.limit_default', 'Default limit of the organization')
                : current.monthly_limit_source === 'platform'
                  ? t('organization.member.limit_platform', "Platform administrator's limit")
                  : t('organization.member.limit_none', 'No monthly limit')
          }
        >
          <LimitBar spent={moneyAmount(current, 'spent_month')} limit={limit} />
        </StatCard>
        <StatCard
          label={t('organization.members.alltime', 'All-time')}
          value={amount('spent_alltime')}
          note={t('organization.member.since_joined', 'Since the member joined')}
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
            formatTick={(value) => money.format(value, AXIS_AMOUNT, spendCurrency)}
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
          <MemberProjects
            detail={data}
            member={current}
            canRevoke={actions.access}
            onAction={onAction}
          />
          <MemberModelsOff detail={data} />
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
