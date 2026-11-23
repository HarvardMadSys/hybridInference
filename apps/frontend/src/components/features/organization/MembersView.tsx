'use client';

import { useEffect, useState } from 'react';
import { useAuth } from '@/components/providers';
import { useT } from '@/components/providers/useT';
import {
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
} from '@/components/features/admin/AdminPrimitives';
import {
  NOTICE_ACTION,
  PANEL,
  TABLE_CELL,
  TABLE_HEAD_CELL,
} from '@/components/features/admin/adminStyles';
import { SearchBox } from '@/components/features/console-shared/SearchBox';
import { SpendCells } from '@/components/features/console-shared/SpendCells';
import { StatusPill } from '@/components/features/console-shared/StatusPill';
import { SummaryCard } from '@/components/features/console-shared/SummaryCard';
import { ThresholdInput } from '@/components/features/console-shared/ThresholdInput';
import { Select } from '@/components/ui/Select';
import type {
  MemberLimitState,
  MemberSort,
  MemberStatusFilter,
  MemberSummaryCard,
  OrganizationMember,
} from '@/lib/api/organization';
import {
  useOrganization,
  useOrganizationMembers,
  useOrganizationMembersSummary,
} from '@/lib/hooks';
import { useMoney } from '@/lib/hooks/useMoney';
import { useFormatter } from '@/lib/i18n/useFormatter';
import { getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { moneyAmount } from '@/lib/utils/money';
import { MemberDetailPanel } from './MemberDetailPanel';
import { MemberDialogs, type MemberDialog } from './MemberDialogs';
import { MemberDrawer } from './MemberDrawer';
import { LimitMeter, Pager, useLastPage, useListPosition } from './parts';
import { limitUse, memberName, memberStatus, relativeTime } from './text';

const PAGE_SIZE = 50;
const SEARCH_DELAY_MS = 300;
/** How many members a card names: all that the gateway sends for it. */
const CARD_MEMBERS = 5;
/** The period of a member's details: the requests, the tokens and the models of its panel. */
const DETAIL_PERIOD = '30d';

type StatusChoice = MemberStatusFilter | 'all';
type LimitChoice = MemberLimitState | 'any';
interface Order {
  by: MemberSort;
  order: 'asc' | 'desc';
}
/** What the list is narrowed to and how it is ordered; the search text is beside it. */
interface Filters {
  status: StatusChoice;
  limitState: LimitChoice;
  minCostToday: number | null;
  minCostMonth: number | null;
  order: Order;
}

const DEFAULT_ORDER: Order = { by: 'month', order: 'desc' };
const NO_FILTERS: Filters = {
  status: 'all',
  limitState: 'any',
  minCostToday: null,
  minCostMonth: null,
  order: DEFAULT_ORDER,
};
/** The list a card opens: the members at 80% of their limit or more, the nearest first. */
const NEAR_LIMIT: Filters = {
  ...NO_FILTERS,
  limitState: 'near',
  order: { by: 'limit_use', order: 'desc' },
};
/** The list a card opens: every member, the largest spend of today first. */
const TOP_TODAY: Filters = { ...NO_FILTERS, order: { by: 'today', order: 'desc' } };

function sameFilters(a: Filters, b: Filters): boolean {
  return (
    a.status === b.status &&
    a.limitState === b.limitState &&
    a.minCostToday === b.minCostToday &&
    a.minCostMonth === b.minCostMonth &&
    a.order.by === b.order.by &&
    a.order.order === b.order.order
  );
}

const BADGE = 'rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide';
// A figure's cell in this table: the size of its other cells, and no color of
// its own, because the spend cells color a figure by whether it is zero.
const SPEND_NUMBER = 'whitespace-nowrap px-4 py-2.5 text-right text-[13px] tabular-nums';

/**
 * The members of the organization, one row each, with what each spent today,
 * in the last 7 days, in this month and in all time, and where its month
 * stands against its limit. Two cards above the list open it on the members
 * near their limit and on today's largest spenders. A row opens a panel at the
 * right side of the window, with the member's details and the actions on it;
 * the list stays behind the panel.
 */
export function MembersView() {
  const t = useT();
  const money = useMoney();
  const fmt = useFormatter();
  const { state } = useAuth();
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const { status, limitState, minCostToday, minCostMonth, order } = filters;
  // Another search, filter or order starts at the first page.
  const [page, setPage] = useListPosition(
    `${search}|${status}|${limitState}|${minCostToday}|${minCostMonth}|${order.by}|${order.order}`,
    0,
  );
  // The member whose panel is open, as its row had it. The panel reads the
  // member again itself, so it stays right after an action, and stays open when
  // a filter no longer lists the member (a removal under "Active").
  const [opened, setOpened] = useState<OrganizationMember | null>(null);
  const [dialog, setDialog] = useState<MemberDialog | null>(null);

  // The list is asked for once typing pauses.
  useEffect(() => {
    const timer = setTimeout(() => setSearch(searchInput.trim()), SEARCH_DELAY_MS);
    return () => clearTimeout(timer);
  }, [searchInput]);

  const organization = useOrganization();
  const summary = useOrganizationMembersSummary();
  const { data, error, isLoading, isPlaceholderData, refetch } = useOrganizationMembers({
    search: search || undefined,
    status: status === 'all' ? undefined : status,
    limitState: limitState === 'any' ? undefined : limitState,
    minCostToday: minCostToday ?? undefined,
    minCostMonth: minCostMonth ?? undefined,
    // The unit the thresholds were read in: the gateway refuses another one.
    currency: money.currency,
    sortBy: order.by,
    order: order.order,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  });
  const members = data?.members ?? [];
  const total = data?.total ?? 0;

  useLastPage(page, setPage, data?.total, PAGE_SIZE);

  const statusOptions: ReadonlyArray<{ value: StatusChoice; label: string }> = [
    { value: 'all', label: t('organization.members.status_all', 'All statuses') },
    { value: 'active', label: t('organization.status.active', 'Active') },
    { value: 'pending_approval', label: t('organization.status.pending', 'Pending approval') },
    { value: 'removed', label: t('organization.status.removed', 'Removed') },
    { value: 'suspended', label: t('organization.status.suspended', 'Suspended') },
  ];
  const limitOptions: ReadonlyArray<{ value: LimitChoice; label: string }> = [
    { value: 'any', label: t('organization.members.limit_any', 'Any limit use') },
    { value: 'near', label: t('organization.members.limit_near', 'Near the limit (≥80%)') },
    { value: 'over', label: t('organization.members.limit_over', 'Over the limit') },
  ];
  const trendLabel = t('organization.members.trend_label', 'Spend of the last 7 days');

  /** A card opens its list from the first page, with nothing else narrowing it. */
  function openView(view: Filters) {
    setSearchInput('');
    setSearch('');
    setFilters(view);
  }

  function sortBy(by: MemberSort) {
    setFilters((current) => ({
      ...current,
      order:
        current.order.by === by
          ? { by, order: current.order.order === 'asc' ? 'desc' : 'asc' }
          : // A name reads from A; a figure from its largest.
            { by, order: by === 'name' || by === 'status' ? 'asc' : 'desc' },
    }));
  }

  function header(by: MemberSort, label: string, numeric = false) {
    const active = order.by === by;
    return (
      <th
        scope="col"
        aria-sort={active ? (order.order === 'asc' ? 'ascending' : 'descending') : undefined}
        className={`${TABLE_HEAD_CELL} ${numeric ? 'text-right' : ''}`}
      >
        <button
          type="button"
          onClick={() => sortBy(by)}
          className={`inline-flex items-center gap-1 uppercase tracking-wide transition hover:text-gray-900 ${
            active ? 'text-gray-900' : ''
          }`}
        >
          {label}
          {active ? <span aria-hidden="true">{order.order === 'asc' ? '↑' : '↓'}</span> : null}
        </button>
      </th>
    );
  }

  /** The members of a card, each with the figure that put it there. */
  function cardItems(
    card: MemberSummaryCard | undefined,
    figureOf: (member: OrganizationMember) => string,
  ) {
    return (card?.top ?? []).map((member) => ({
      id: member.user_id,
      label: memberName(member),
      value: figureOf(member),
    }));
  }

  function figure(member: OrganizationMember, field: string) {
    return {
      amount: moneyAmount(member, field) ?? 0,
      text: money.value(member, field, { maximumFractionDigits: 2 }),
    };
  }

  function row(member: OrganizationMember) {
    const open = opened?.user_id === member.user_id;
    const name = memberName(member);
    const you = member.user_id === state.user?.id;
    return (
      <tr
        key={member.user_id}
        onClick={() => setOpened(member)}
        className={`cursor-pointer border-b border-gray-100 transition last:border-b-0 ${
          open ? 'bg-gray-50' : 'hover:bg-gray-50'
        }`}
      >
        <td className={TABLE_CELL}>
          <div className="flex flex-wrap items-center gap-1.5">
            {/* The row's one control for the keyboard and a screen reader. */}
            <button
              type="button"
              aria-haspopup="dialog"
              aria-expanded={open}
              onClick={(event) => {
                event.stopPropagation();
                setOpened(member);
              }}
              className="max-w-[18rem] truncate text-left font-medium text-gray-900 hover:underline"
            >
              {name}
            </button>
            {member.role === 'admin' ? (
              <span className={`${BADGE} bg-gray-900 text-white`}>
                {t('organization.members.admin_badge', 'Admin')}
              </span>
            ) : null}
            {you ? (
              <span className={`${BADGE} bg-gray-100 text-gray-600`}>
                {t('organization.members.you_badge', 'You')}
              </span>
            ) : null}
          </div>
          {member.email && member.email !== name ? (
            <div className="max-w-[20rem] truncate text-[12px] text-gray-500">{member.email}</div>
          ) : null}
        </td>
        <td className={TABLE_CELL}>
          <StatusPill tone={memberStatus(member, t)} />
        </td>
        <td className={`${TABLE_CELL} whitespace-nowrap text-gray-500`}>
          {relativeTime(member.last_request_at, t, fmt)}
        </td>
        <SpendCells
          today={figure(member, 'spent_today')}
          trend={(member.spend_days ?? []).map((day) => moneyAmount(day, 'cost'))}
          trendLabel={trendLabel}
          trendTitle={fill(t('organization.members.week_total', 'Last 7 days: {amount}'), {
            amount: money.value(member, 'spent_7d', { maximumFractionDigits: 2 }),
          })}
          month={figure(member, 'spent_month')}
          alltime={figure(member, 'spent_alltime')}
          numberClass={SPEND_NUMBER}
        />
        <td className={`${TABLE_CELL} text-right`}>
          <LimitMeter
            spent={moneyAmount(member, 'spent_month')}
            limit={moneyAmount(member, 'monthly_limit')}
            limitText={money.value(member, 'monthly_limit', { maximumFractionDigits: 2 })}
            source={member.monthly_limit_source}
          />
        </td>
      </tr>
    );
  }

  const narrowed =
    search !== '' ||
    status !== 'all' ||
    limitState !== 'any' ||
    minCostToday !== null ||
    minCostMonth !== null;

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <SummaryCard
          title={t('organization.members.card_near', 'Near or over the limit')}
          accent="amber"
          count={summary.data?.near_limit.count ?? 0}
          items={cardItems(summary.data?.near_limit, (member) => {
            const use = limitUse(
              moneyAmount(member, 'spent_month'),
              moneyAmount(member, 'monthly_limit'),
            );
            return use ? `${use.percent}%` : '';
          })}
          empty={t('organization.members.card_empty', 'No members right now')}
          shown={CARD_MEMBERS}
          loading={summary.isLoading}
          active={search === '' && sameFilters(filters, NEAR_LIMIT)}
          onClick={() => openView(NEAR_LIMIT)}
        />
        <SummaryCard
          title={t('organization.members.card_today', 'Top spenders today')}
          accent="emerald"
          count={summary.data?.top_spenders_today.count ?? 0}
          items={cardItems(summary.data?.top_spenders_today, (member) =>
            money.value(member, 'spent_today', { maximumFractionDigits: 2 }),
          )}
          empty={t('organization.members.card_empty', 'No members right now')}
          shown={CARD_MEMBERS}
          loading={summary.isLoading}
          active={search === '' && sameFilters(filters, TOP_TODAY)}
          onClick={() => openView(TOP_TODAY)}
        />
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <SearchBox
          value={searchInput}
          onChange={setSearchInput}
          placeholder={t('organization.members.search', 'Search by name or email')}
          label={t('organization.members.search', 'Search by name or email')}
        />
        <Select
          size="sm"
          className="w-44"
          value={status}
          onValueChange={(next) => setFilters((current) => ({ ...current, status: next }))}
          options={statusOptions}
          aria-label={t('organization.members.status_filter', 'Status')}
        />
        <Select
          size="sm"
          className="w-52"
          value={limitState}
          onValueChange={(next) => setFilters((current) => ({ ...current, limitState: next }))}
          options={limitOptions}
          aria-label={t('organization.members.limit_filter', 'Limit use')}
        />
        <ThresholdInput
          label={fill(t('organization.members.min_today', 'Today ≥ {symbol}'), {
            symbol: money.symbol,
          })}
          value={minCostToday}
          onChange={(next) => setFilters((current) => ({ ...current, minCostToday: next }))}
          step="0.01"
        />
        <ThresholdInput
          label={fill(t('organization.members.min_month', 'Month ≥ {symbol}'), {
            symbol: money.symbol,
          })}
          value={minCostMonth}
          onChange={(next) => setFilters((current) => ({ ...current, minCostMonth: next }))}
          step="0.01"
        />
      </div>

      {isLoading ? (
        <AdminLoading label={t('common.loading', 'Loading...')} />
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
          <div
            className={`${PANEL} overflow-x-auto transition-opacity ${
              isPlaceholderData ? 'opacity-60' : ''
            }`}
          >
            <table className="min-w-full">
              <thead>
                <tr className="border-b border-gray-200 bg-gray-50">
                  {header('name', t('organization.members.member', 'Member'))}
                  {header('status', t('organization.members.status', 'Status'))}
                  {header('last_request', t('organization.members.last_request', 'Last request'))}
                  {header('today', t('organization.members.today', 'Today'), true)}
                  {header('week', t('organization.members.week', '7 days'))}
                  {header('month', t('organization.member.this_month', 'This month'), true)}
                  {header('alltime', t('organization.members.alltime', 'All-time'), true)}
                  {/* By the share of the limit that the month used, which is what the column shows. */}
                  {header(
                    'limit_use',
                    t('organization.members.monthly_limit', 'Monthly limit'),
                    true,
                  )}
                </tr>
              </thead>
              <tbody>{members.map(row)}</tbody>
            </table>
            {members.length === 0 ? (
              <AdminEmptyState
                variant="plain"
                title={
                  narrowed
                    ? t('organization.members.no_match', 'No member matches these filters.')
                    : t('organization.members.empty', 'The organization has no members yet.')
                }
              />
            ) : null}
          </div>
          <Pager
            page={page}
            pageSize={PAGE_SIZE}
            total={total}
            count={members.length}
            onPageChange={setPage}
          />
        </>
      )}

      {opened ? (
        <MemberDrawer
          key={opened.user_id}
          title={memberName(opened)}
          subtitle={opened.email && opened.email !== memberName(opened) ? opened.email : null}
          // A confirmation opens over the panel and has the keys while it is open.
          suspended={dialog !== null}
          onClose={() => setOpened(null)}
        >
          <MemberDetailPanel member={opened} period={DETAIL_PERIOD} onAction={setDialog} />
        </MemberDrawer>
      ) : null}

      <MemberDialogs
        dialog={dialog}
        organization={organization.data}
        onRetryOrganization={organization.isError ? () => void organization.refetch() : undefined}
        onClose={() => setDialog(null)}
      />
    </div>
  );
}
