'use client';

import { Fragment, useEffect, useId, useState } from 'react';
import { useAuth } from '@/components/providers';
import { useLocale } from '@/components/providers/LocaleProvider';
import { useT } from '@/components/providers/useT';
import {
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
} from '@/components/features/admin/AdminPrimitives';
import {
  BUTTON_GHOST,
  CONTROL,
  NOTICE_ACTION,
  PANEL,
  TABLE_CELL,
  TABLE_HEAD_CELL,
} from '@/components/features/admin/adminStyles';
import {
  AuditEntryDetails,
  DisclosureChevron,
  type AuditMeta,
} from '@/components/features/console-shared/AuditEntryDetails';
import { Select } from '@/components/ui/Select';
import type { AuditCategory, AuditEntry, OrganizationPeriod } from '@/lib/api/organization';
import { useOrganizationAudit } from '@/lib/hooks';
import { useFormatter } from '@/lib/i18n/useFormatter';
import { getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { Pager, PeriodControl, useLastPage, useListPosition } from './parts';
import { auditActor, auditDetails, auditEvent, auditTarget } from './text';

const PAGE_SIZE = 50;
const SEARCH_DELAY_MS = 300;

type CategoryChoice = AuditCategory | 'all';
/** The member the log is narrowed to: its account, and how the page names it. */
interface MemberChoice {
  id: string;
  label: string;
}

/** A link-like control in a cell: the member of the row, to narrow the log to. */
const MEMBER_LINK =
  'max-w-full truncate text-left underline decoration-gray-300 underline-offset-2 transition hover:text-gray-900 hover:decoration-gray-500';

/**
 * The management actions of this organization, newest first: who did what to
 * whom, and what changed. A row opens to show every field that the action
 * recorded. A platform administrator is named as that and nothing more; its
 * account is the deployment's, and no row shows where an action came from.
 */
export function AuditLogView() {
  const t = useT();
  const fmt = useFormatter();
  const locale = useLocale() === 'zh-CN' ? 'zh-CN' : 'en-US';
  const { state } = useAuth();
  const baseId = useId();
  const organizationName = state.user?.organization?.name ?? '';
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');
  const [category, setCategory] = useState<CategoryChoice>('all');
  const [member, setMember] = useState<MemberChoice | null>(null);
  // The log is short and its rows are rare: it opens on all of them.
  const [period, setPeriod] = useState<OrganizationPeriod | null>(null);
  // Another search, kind of event, member or period starts at the first page.
  const [page, setPage] = useListPosition(
    `${period ?? 'all'}|${category}|${member?.id ?? ''}|${search}`,
    0,
  );
  const [opened, setOpened] = useState<ReadonlySet<number>>(new Set());

  useEffect(() => {
    const timer = setTimeout(() => setSearch(searchInput.trim()), SEARCH_DELAY_MS);
    return () => clearTimeout(timer);
  }, [searchInput]);

  const { data, error, isLoading, isPlaceholderData, refetch } = useOrganizationAudit({
    period: period ?? undefined,
    category: category === 'all' ? undefined : category,
    member: member?.id,
    search: search || undefined,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  });
  useLastPage(page, setPage, data?.total, PAGE_SIZE);

  const categories: ReadonlyArray<{ value: CategoryChoice; label: string }> = [
    { value: 'all', label: t('organization.audit.all', 'All events') },
    { value: 'membership', label: t('organization.audit.membership', 'Membership') },
    { value: 'limits', label: t('organization.audit.limits', 'Limits') },
    { value: 'access', label: t('organization.audit.access', 'Access') },
    { value: 'organization', label: t('organization.audit.organization', 'Organization') },
  ];

  function toggle(id: number) {
    setOpened((current) => {
      const next = new Set(current);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  }

  /** A member of a row as a control that narrows the log to it; other text as it is. */
  function memberCell(id: string | null | undefined, label: string) {
    if (!id || member?.id === id) return label;
    return (
      <button
        type="button"
        className={MEMBER_LINK}
        title={fill(t('organization.audit.only_member', 'Show only the events of {member}'), {
          member: label,
        })}
        onClick={() => setMember({ id, label })}
      >
        {label}
      </button>
    );
  }

  /** The lines of an opened entry. No line has the source address: the gateway sends none. */
  function meta(entry: AuditEntry): AuditMeta[] {
    return [
      [t('organization.audit.event', 'Event'), auditEvent(entry.action, t)],
      [
        t('organization.requests.time', 'Time'),
        <time key="time" dateTime={entry.timestamp}>
          {fmt.dateTime(entry.timestamp, {
            year: 'numeric',
            month: 'short',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
          })}
        </time>,
      ],
      [t('organization.audit.actor', 'Actor'), auditActor(entry, t)],
      [t('organization.audit.target', 'Target'), auditTarget(entry, organizationName)],
    ];
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="search"
          value={searchInput}
          onChange={(event) => setSearchInput(event.target.value)}
          placeholder={t('organization.audit.search', 'Search the log')}
          aria-label={t('organization.audit.search', 'Search the log')}
          className={`${CONTROL} min-w-[14rem] flex-1`}
        />
        <Select
          size="sm"
          className="w-44"
          value={category}
          onValueChange={setCategory}
          options={categories}
          aria-label={t('organization.audit.event', 'Event')}
        />
        <PeriodControl value={period} onChange={setPeriod} onAll={() => setPeriod(null)} />
      </div>
      {member ? (
        <div className="flex flex-wrap items-center gap-2 text-[12px] text-gray-600">
          <span>
            {fill(t('organization.audit.member_filter', 'Only the events of {member}'), {
              member: member.label,
            })}
          </span>
          <button type="button" className={BUTTON_GHOST} onClick={() => setMember(null)}>
            {t('organization.audit.member_clear', 'Show every member')}
          </button>
        </div>
      ) : null}

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
      ) : data.entries.length === 0 ? (
        <AdminEmptyState
          title={
            search || category !== 'all' || period || member
              ? t('organization.audit.no_match', 'No event matches these filters.')
              : t('organization.audit.empty', 'Nothing has been recorded yet.')
          }
        />
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
                  <th scope="col" className={TABLE_HEAD_CELL}>
                    {t('organization.requests.time', 'Time')}
                  </th>
                  <th scope="col" className={TABLE_HEAD_CELL}>
                    {t('organization.audit.actor', 'Actor')}
                  </th>
                  <th scope="col" className={TABLE_HEAD_CELL}>
                    {t('organization.audit.event', 'Event')}
                  </th>
                  <th scope="col" className={TABLE_HEAD_CELL}>
                    {t('organization.audit.target', 'Target')}
                  </th>
                  <th scope="col" className={TABLE_HEAD_CELL}>
                    {t('organization.audit.details', 'Details')}
                  </th>
                  <th scope="col" className={TABLE_HEAD_CELL}>
                    <span className="sr-only">
                      {t('organization.audit.all_fields', 'Every field')}
                    </span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {data.entries.map((entry) => {
                  const isOpen = opened.has(entry.id);
                  const detailsId = `${baseId}-entry-${entry.id}`;
                  const event = auditEvent(entry.action, t);
                  return (
                    <Fragment key={entry.id}>
                      <tr
                        className={`border-b border-gray-100 last:border-b-0 ${
                          isOpen ? 'border-b-0 bg-gray-50' : ''
                        }`}
                      >
                        <td className={`${TABLE_CELL} whitespace-nowrap text-gray-500`}>
                          {fmt.dateTime(entry.timestamp, {
                            month: 'short',
                            day: 'numeric',
                            hour: '2-digit',
                            minute: '2-digit',
                          })}
                        </td>
                        <td className={`${TABLE_CELL} max-w-[16rem] truncate`}>
                          {memberCell(
                            entry.actor === 'member' ? entry.actor_user_id : null,
                            auditActor(entry, t),
                          )}
                        </td>
                        <td className={`${TABLE_CELL} font-medium text-gray-900`}>{event}</td>
                        <td className={`${TABLE_CELL} max-w-[16rem] truncate`}>
                          {memberCell(
                            entry.target_type === 'organization' ? null : entry.target_user_id,
                            auditTarget(entry, organizationName),
                          )}
                        </td>
                        <td className={`${TABLE_CELL} text-gray-500`}>
                          {auditDetails(entry, t, locale) || '—'}
                        </td>
                        <td className="py-2 pr-3 text-right">
                          <button
                            type="button"
                            onClick={() => toggle(entry.id)}
                            aria-expanded={isOpen}
                            aria-controls={isOpen ? detailsId : undefined}
                            aria-label={fill(
                              isOpen
                                ? t(
                                    'organization.audit.hide_fields',
                                    'Hide every field of {event}, {time}',
                                  )
                                : t(
                                    'organization.audit.show_fields',
                                    'Show every field of {event}, {time}',
                                  ),
                              {
                                event,
                                time: fmt.dateTime(entry.timestamp, {
                                  month: 'short',
                                  day: 'numeric',
                                  hour: '2-digit',
                                  minute: '2-digit',
                                  second: '2-digit',
                                }),
                              },
                            )}
                            className="inline-flex h-7 w-7 items-center justify-center rounded-md text-gray-400 transition-colors duration-150 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400"
                          >
                            <DisclosureChevron open={isOpen} />
                          </button>
                        </td>
                      </tr>
                      {isOpen ? (
                        <tr id={detailsId} className="border-b border-gray-100 bg-gray-50">
                          <td colSpan={6} className="px-4 pb-4 pt-1">
                            <AuditEntryDetails
                              meta={meta(entry)}
                              details={entry.details ?? null}
                              raw={false}
                            />
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
          <Pager
            page={page}
            pageSize={PAGE_SIZE}
            total={data.total}
            count={data.entries.length}
            onPageChange={setPage}
          />
        </>
      )}
    </div>
  );
}
