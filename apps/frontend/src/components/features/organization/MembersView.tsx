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
import { StatusPill } from '@/components/features/console-shared/StatusPill';
import { Select } from '@/components/ui/Select';
import type { MemberSort, MemberStatusFilter, OrganizationMember } from '@/lib/api/organization';
import { useOrganization, useOrganizationMembers } from '@/lib/hooks';
import { useMoney } from '@/lib/hooks/useMoney';
import { useFormatter } from '@/lib/i18n/useFormatter';
import { getErrorMessage } from '@/lib/utils/errors';
import { moneyAmount } from '@/lib/utils/money';
import { MemberDetailPanel } from './MemberDetailPanel';
import { MemberDialogs, type MemberDialog } from './MemberDialogs';
import { MemberDrawer } from './MemberDrawer';
import {
  LimitMeter,
  Pager,
  PeriodControl,
  useLastPage,
  useListPosition,
  useOrganizationPeriod,
} from './parts';
import { compactNumber, memberName, memberStatus, relativeTime } from './text';

const PAGE_SIZE = 50;
const SEARCH_DELAY_MS = 300;

type StatusChoice = MemberStatusFilter | 'all';
interface Order {
  by: MemberSort;
  order: 'asc' | 'desc';
}

const BADGE = 'rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide';

/**
 * The members of the organization, one row each, with what each used in the
 * period and where its month stands against its limit. A row opens a panel at
 * the right side of the window, with the member's details and the actions on
 * it; the list stays behind the panel.
 */
export function MembersView() {
  const t = useT();
  const money = useMoney();
  const fmt = useFormatter();
  const { state } = useAuth();
  const { period, setPeriod } = useOrganizationPeriod();
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState<StatusChoice>('all');
  const [order, setOrder] = useState<Order>({ by: 'spend', order: 'desc' });
  // Another period, search, status or order starts at the first page.
  const [page, setPage] = useListPosition(
    `${period}|${search}|${status}|${order.by}|${order.order}`,
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
  const { data, error, isLoading, isPlaceholderData, refetch } = useOrganizationMembers({
    period,
    search: search || undefined,
    status: status === 'all' ? undefined : status,
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

  function sortBy(by: MemberSort) {
    setOrder((current) =>
      current.by === by
        ? { by, order: current.order === 'asc' ? 'desc' : 'asc' }
        : // A name reads from A; a figure from its largest.
          { by, order: by === 'name' || by === 'status' ? 'asc' : 'desc' },
    );
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
        <td className={`${TABLE_CELL} text-right tabular-nums`}>{fmt.number(member.requests)}</td>
        <td className={`${TABLE_CELL} text-right tabular-nums`}>
          {compactNumber(member.total_tokens, fmt)}
        </td>
        <td className={`${TABLE_CELL} text-right tabular-nums text-gray-900`}>
          {money.value(member, 'cost', { maximumFractionDigits: 2 })}
        </td>
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

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <PeriodControl value={period} onChange={setPeriod} />
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
          onValueChange={setStatus}
          options={statusOptions}
          aria-label={t('organization.members.status_filter', 'Status')}
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
                  {header('requests', t('organization.overview.requests', 'Requests'), true)}
                  {header('tokens', t('organization.overview.tokens', 'Tokens'), true)}
                  {header('spend', t('organization.overview.spend', 'Spend'), true)}
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
                  search || status !== 'all'
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
          <MemberDetailPanel member={opened} period={period} onAction={setDialog} />
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
