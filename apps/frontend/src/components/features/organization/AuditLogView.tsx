'use client';

import { useEffect, useState } from 'react';
import { useAuth } from '@/components/providers';
import { useLocale } from '@/components/providers/LocaleProvider';
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
import type { AuditCategory, OrganizationPeriod } from '@/lib/api/organization';
import { useOrganizationAudit } from '@/lib/hooks';
import { useFormatter } from '@/lib/i18n/useFormatter';
import { getErrorMessage } from '@/lib/utils/errors';
import { Pager, PeriodControl } from './parts';
import { auditActor, auditDetails, auditEvent, auditTarget } from './text';

const PAGE_SIZE = 50;
const SEARCH_DELAY_MS = 300;

type CategoryChoice = AuditCategory | 'all';

/**
 * The management actions of this organization, newest first: who did what to
 * whom, and what changed. A platform administrator is named as that and
 * nothing more; its account is the deployment's.
 */
export function AuditLogView() {
  const t = useT();
  const fmt = useFormatter();
  const locale = useLocale() === 'zh-CN' ? 'zh-CN' : 'en-US';
  const { state } = useAuth();
  const organizationName = state.user?.organization?.name ?? '';
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');
  const [category, setCategory] = useState<CategoryChoice>('all');
  // The log is short and its rows are rare: it opens on all of them.
  const [period, setPeriod] = useState<OrganizationPeriod | null>(null);
  const [page, setPage] = useState(0);

  useEffect(() => {
    const timer = setTimeout(() => {
      setSearch(searchInput.trim());
      setPage(0);
    }, SEARCH_DELAY_MS);
    return () => clearTimeout(timer);
  }, [searchInput]);

  const { data, error, isLoading, isPlaceholderData, refetch } = useOrganizationAudit({
    period: period ?? undefined,
    category: category === 'all' ? undefined : category,
    search: search || undefined,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  });

  const categories: ReadonlyArray<{ value: CategoryChoice; label: string }> = [
    { value: 'all', label: t('organization.audit.all', 'All events') },
    { value: 'membership', label: t('organization.audit.membership', 'Membership') },
    { value: 'limits', label: t('organization.audit.limits', 'Limits') },
    { value: 'organization', label: t('organization.audit.organization', 'Organization') },
  ];

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
          onValueChange={(value) => {
            setCategory(value);
            setPage(0);
          }}
          options={categories}
          aria-label={t('organization.audit.event', 'Event')}
        />
        <PeriodControl
          value={period}
          onChange={(next) => {
            setPeriod(next);
            setPage(0);
          }}
          onAll={() => {
            setPeriod(null);
            setPage(0);
          }}
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
      ) : data.entries.length === 0 ? (
        <AdminEmptyState
          title={
            search || category !== 'all' || period
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
                </tr>
              </thead>
              <tbody>
                {data.entries.map((entry) => (
                  <tr key={entry.id} className="border-b border-gray-100 last:border-b-0">
                    <td className={`${TABLE_CELL} whitespace-nowrap text-gray-500`}>
                      {fmt.dateTime(entry.timestamp, {
                        month: 'short',
                        day: 'numeric',
                        hour: '2-digit',
                        minute: '2-digit',
                      })}
                    </td>
                    <td className={`${TABLE_CELL} max-w-[16rem] truncate`}>
                      {auditActor(entry, t)}
                    </td>
                    <td className={`${TABLE_CELL} font-medium text-gray-900`}>
                      {auditEvent(entry.action, t)}
                    </td>
                    <td className={`${TABLE_CELL} max-w-[16rem] truncate`}>
                      {auditTarget(entry, organizationName)}
                    </td>
                    <td className={`${TABLE_CELL} text-gray-500`}>
                      {auditDetails(entry, t, locale) || '—'}
                    </td>
                  </tr>
                ))}
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
