'use client';

import { Fragment, useEffect, useState } from 'react';
import { useAuth } from '@/components/providers';
import { useT } from '@/components/providers/useT';
import {
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
} from '@/components/features/admin/AdminPrimitives';
import {
  CONTROL,
  NOTICE_ACTION,
  PANEL,
  TABLE_CELL,
  TABLE_HEAD_CELL,
} from '@/components/features/admin/adminStyles';
import { Select } from '@/components/ui/Select';
import type { MemberSort, MemberStatusFilter, OrganizationMember } from '@/lib/api/organization';
import { useOrganization, useOrganizationMembers } from '@/lib/hooks';
import { useMoney } from '@/lib/hooks/useMoney';
import { useFormatter } from '@/lib/i18n/useFormatter';
import { getErrorMessage } from '@/lib/utils/errors';
import { moneyAmount } from '@/lib/utils/money';
import { MemberDetailPanel } from './MemberDetailPanel';
import { MemberDialogs, type MemberDialog } from './MemberDialogs';
import { LimitMeter, Pager, PeriodControl, StatusPill, useOrganizationPeriod } from './parts';
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
 * period and where its month stands against its limit. A row opens in place,
 * as a user's row does in the admin area, with the member's details and the
 * actions on it.
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
  const [page, setPage] = useState(0);
  const [openId, setOpenId] = useState<string | null>(null);
  const [dialog, setDialog] = useState<MemberDialog | null>(null);

  // The list is asked for once typing pauses, from its first page.
  useEffect(() => {
    const timer = setTimeout(() => {
      setSearch(searchInput.trim());
      setPage(0);
    }, SEARCH_DELAY_MS);
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

  // A removal or a narrower search can leave the page past the last one.
  useEffect(() => {
    if (data && page > 0 && page * PAGE_SIZE >= data.total) {
      setPage(Math.max(0, Math.ceil(data.total / PAGE_SIZE) - 1));
    }
  }, [data, page]);

  const statusOptions: ReadonlyArray<{ value: StatusChoice; label: string }> = [
    { value: 'all', label: t('organization.members.status_all', 'All statuses') },
    { value: 'active', label: t('organization.status.active', 'Active') },
    { value: 'pending_approval', label: t('organization.status.pending', 'Pending approval') },
    { value: 'removed', label: t('organization.status.removed', 'Removed') },
    { value: 'suspended', label: t('organization.status.suspended', 'Suspended') },
  ];

  function sortBy(by: MemberSort) {
    setPage(0);
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
    const open = openId === member.user_id;
    const name = memberName(member);
    const you = member.user_id === state.user?.id;
    return (
      <Fragment key={member.user_id}>
        <tr
          onClick={() => setOpenId(open ? null : member.user_id)}
          aria-expanded={open}
          className={`cursor-pointer border-b border-gray-100 transition ${
            open ? 'bg-gray-50' : 'hover:bg-gray-50'
          }`}
        >
          <td className={TABLE_CELL}>
            <div className="flex items-center gap-2">
              <svg
                className={`h-3.5 w-3.5 shrink-0 text-gray-400 transition ${open ? 'rotate-90' : ''}`}
                viewBox="0 0 20 20"
                fill="currentColor"
                aria-hidden="true"
              >
                <path
                  fillRule="evenodd"
                  d="M7.21 14.77a.75.75 0 01.02-1.06L11.168 10 7.23 6.29a.75.75 0 111.04-1.08l4.5 4.25a.75.75 0 010 1.08l-4.5 4.25a.75.75 0 01-1.06-.02z"
                  clipRule="evenodd"
                />
              </svg>
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-1.5">
                  {/* The row's one control for the keyboard and a screen reader. */}
                  <button
                    type="button"
                    aria-expanded={open}
                    onClick={(event) => {
                      event.stopPropagation();
                      setOpenId(open ? null : member.user_id);
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
                  <div className="max-w-[20rem] truncate text-[12px] text-gray-500">
                    {member.email}
                  </div>
                ) : null}
              </div>
            </div>
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
            />
          </td>
        </tr>
        {open ? (
          <tr className="border-b border-gray-100">
            <td colSpan={7} className="bg-gray-50 p-0">
              {/* As wide as the visible part of the table and pinned to its
                  left edge: on a phone the table scrolls sideways, and the
                  details would otherwise run off past the right of the screen. */}
              <div className="sticky left-0 w-[100cqw] p-4">
                <MemberDetailPanel member={member} period={period} onAction={setDialog} />
              </div>
            </td>
          </tr>
        ) : null}
      </Fragment>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <PeriodControl
          value={period}
          onChange={(next) => {
            setPeriod(next);
            setPage(0);
          }}
        />
        <div className="relative min-w-[14rem] flex-1">
          <svg
            className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-gray-400"
            viewBox="0 0 20 20"
            fill="currentColor"
            aria-hidden="true"
          >
            <path
              fillRule="evenodd"
              d="M9 3.5a5.5 5.5 0 100 11 5.5 5.5 0 000-11zM2 9a7 7 0 1112.452 4.391l3.328 3.329a.75.75 0 11-1.06 1.06l-3.329-3.328A7 7 0 012 9z"
              clipRule="evenodd"
            />
          </svg>
          <input
            type="search"
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
            placeholder={t('organization.members.search', 'Search by name or email')}
            aria-label={t('organization.members.search', 'Search by name or email')}
            className={`${CONTROL} w-full pl-8`}
          />
        </div>
        <Select
          size="sm"
          className="w-44"
          value={status}
          onValueChange={(value) => {
            setStatus(value);
            setPage(0);
          }}
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
            className={`${PANEL} overflow-x-auto transition-opacity [container-type:inline-size] ${
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
                  {header('month', t('organization.members.monthly_limit', 'Monthly limit'), true)}
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

      <MemberDialogs
        dialog={dialog}
        organization={organization.data}
        onClose={() => setDialog(null)}
      />
    </div>
  );
}
