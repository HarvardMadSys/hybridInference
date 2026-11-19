'use client';

import { useEffect, useId, useState } from 'react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { useT } from '@/components/providers/useT';
import {
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
  AdminSubTabs,
} from '@/components/features/admin/AdminPrimitives';
import {
  BUTTON_SECONDARY,
  CONTROL,
  NOTICE_ACTION,
  PANEL,
  TABLE_CELL,
  TABLE_HEAD_CELL,
} from '@/components/features/admin/adminStyles';
import { Select } from '@/components/ui/Select';
import type { OrganizationPeriod } from '@/lib/api/organization';
import {
  useOrganizationMemberUsage,
  useOrganizationModelUsage,
  useOrganizationOverview,
  useOrganizationRequests,
} from '@/lib/hooks';
import { useMoney } from '@/lib/hooks/useMoney';
import { useFormatter } from '@/lib/i18n/useFormatter';
import { getErrorMessage } from '@/lib/utils/errors';
import { moneyAmount } from '@/lib/utils/money';
import { RequestsTable } from './figures';
import { Pager, PeriodControl, useOrganizationPeriod } from './parts';
import { compactNumber, memberName } from './text';

type UsageKind = 'requests' | 'models' | 'members';
type StatusChoice = 'all' | 'ok' | 'errors';

const REQUEST_PAGE = 50;
const MEMBER_PAGE = 50;
const SEARCH_DELAY_MS = 300;
const ALL_MODELS = '';

const NUM_HEAD = `${TABLE_HEAD_CELL} text-right`;
const NUM_CELL = `${TABLE_CELL} whitespace-nowrap text-right tabular-nums`;

function usageKind(raw: string | null): UsageKind {
  return raw === 'models' || raw === 'members' ? raw : 'requests';
}

/** A part of a whole as a percentage with one decimal; a dash when there is no whole. */
function share(part: number, whole: number): string {
  if (!(whole > 0)) return '—';
  return `${((part / whole) * 100).toFixed(1)}%`;
}

function Failure({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const t = useT();
  return (
    <AdminNotice
      role="alert"
      action={
        <button type="button" className={NOTICE_ACTION} onClick={onRetry}>
          {t('organization.retry', 'Retry')}
        </button>
      }
    >
      {getErrorMessage(error)}
    </AdminNotice>
  );
}

/** One row for each request of a member, newest first, a page at a time. */
function Requests({
  period,
  search,
  model,
  status,
}: {
  period: OrganizationPeriod;
  search: string;
  model: string;
  status: StatusChoice;
}) {
  const t = useT();
  // The gateway pages by the last row it sent, so the way back is the cursors
  // of the pages before. They belong to what was asked for: a change of the
  // period or a filter starts again from the newest row, in the same render.
  const filters = `${period}|${search}|${model}|${status}`;
  const [paging, setPaging] = useState<{ filters: string; cursors: string[] }>({
    filters,
    cursors: [],
  });
  const cursors = paging.filters === filters ? paging.cursors : [];
  const setCursors = (next: string[]) => setPaging({ filters, cursors: next });

  const { data, error, isLoading, isPlaceholderData, refetch } = useOrganizationRequests({
    period,
    search: search || undefined,
    model: model || undefined,
    status: status === 'all' ? undefined : status,
    limit: REQUEST_PAGE,
    before: cursors[cursors.length - 1],
  });

  if (isLoading) return <AdminLoading label={t('common.loading', 'Loading...')} />;
  if (!data) return <Failure error={error} onRetry={() => void refetch()} />;
  const next = data.next_before;
  return (
    <div className={`space-y-2 transition-opacity ${isPlaceholderData ? 'opacity-60' : ''}`}>
      <RequestsTable
        requests={data.requests}
        empty={
          search || model || status !== 'all'
            ? t('organization.usage.no_match', 'No request matches these filters.')
            : t('organization.usage.empty', 'No requests in this period.')
        }
      />
      {cursors.length > 0 || next ? (
        <div className="flex items-center justify-end gap-2 px-1 py-1">
          <button
            type="button"
            className={BUTTON_SECONDARY}
            disabled={cursors.length === 0}
            onClick={() => setCursors(cursors.slice(0, -1))}
          >
            {t('organization.usage.newer', 'Newer')}
          </button>
          <button
            type="button"
            className={BUTTON_SECONDARY}
            disabled={!next}
            onClick={() => next && setCursors([...cursors, next])}
          >
            {t('organization.usage.older', 'Older')}
          </button>
        </div>
      ) : null}
    </div>
  );
}

/** What each model was used for in the period, the largest spend first. */
function ByModel({ period }: { period: OrganizationPeriod }) {
  const t = useT();
  const money = useMoney();
  const fmt = useFormatter();
  const { data, error, isLoading, refetch } = useOrganizationModelUsage(period);
  if (isLoading) return <AdminLoading label={t('common.loading', 'Loading...')} />;
  if (!data) return <Failure error={error} onRetry={() => void refetch()} />;
  if (data.length === 0) {
    return <AdminEmptyState title={t('organization.usage.empty', 'No requests in this period.')} />;
  }
  const rows = [...data].sort(
    (a, b) => (moneyAmount(b, 'cost') ?? 0) - (moneyAmount(a, 'cost') ?? 0),
  );
  const total = rows.reduce((sum, row) => sum + (moneyAmount(row, 'cost') ?? 0), 0);
  return (
    <div className={`${PANEL} overflow-x-auto`}>
      <table className="min-w-full">
        <thead>
          <tr className="border-b border-gray-200 bg-gray-50">
            <th scope="col" className={TABLE_HEAD_CELL}>
              {t('organization.requests.model', 'Model')}
            </th>
            <th scope="col" className={NUM_HEAD}>
              {t('organization.overview.requests', 'Requests')}
            </th>
            <th scope="col" className={NUM_HEAD}>
              {t('organization.usage.errors', 'Errors')}
            </th>
            <th scope="col" className={NUM_HEAD}>
              {t('organization.requests.input_tokens', 'Input tokens')}
            </th>
            <th scope="col" className={NUM_HEAD}>
              {t('organization.requests.output_tokens', 'Output tokens')}
            </th>
            <th scope="col" className={NUM_HEAD}>
              {t('organization.overview.spend', 'Spend')}
            </th>
            <th scope="col" className={NUM_HEAD}>
              {t('organization.usage.share', 'Share of spend')}
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.model_id} className="border-b border-gray-100 last:border-b-0">
              <td className={`${TABLE_CELL} max-w-[20rem] truncate font-medium text-gray-900`}>
                {row.model_id}
              </td>
              <td className={NUM_CELL}>{fmt.number(row.requests)}</td>
              <td className={NUM_CELL}>{fmt.number(row.errors)}</td>
              <td className={NUM_CELL}>{compactNumber(row.prompt_tokens, fmt)}</td>
              <td className={NUM_CELL}>{compactNumber(row.completion_tokens, fmt)}</td>
              <td className={`${NUM_CELL} text-gray-900`}>
                {money.value(row, 'cost', { maximumFractionDigits: 2 })}
              </td>
              <td className={NUM_CELL}>{share(moneyAmount(row, 'cost') ?? 0, total)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** What each member used in the period, the largest spend first. */
function ByMember({ period, search }: { period: OrganizationPeriod; search: string }) {
  const t = useT();
  const money = useMoney();
  const fmt = useFormatter();
  // The page belongs to what was asked for: another period or search starts at the first.
  const filters = `${period}|${search}`;
  const [paging, setPaging] = useState({ filters, page: 0 });
  const page = paging.filters === filters ? paging.page : 0;
  const setPage = (next: number) => setPaging({ filters, page: next });
  const overview = useOrganizationOverview(period);
  const { data, error, isLoading, isPlaceholderData, refetch } = useOrganizationMemberUsage({
    period,
    search: search || undefined,
    limit: MEMBER_PAGE,
    offset: page * MEMBER_PAGE,
  });
  if (isLoading) return <AdminLoading label={t('common.loading', 'Loading...')} />;
  if (!data) return <Failure error={error} onRetry={() => void refetch()} />;
  if (data.members.length === 0) {
    return (
      <AdminEmptyState
        title={
          search
            ? t('organization.members.no_match', 'No member matches these filters.')
            : t('organization.members.empty', 'The organization has no members yet.')
        }
      />
    );
  }
  const total = moneyAmount(overview.data, 'cost') ?? 0;
  return (
    <div className={`space-y-2 transition-opacity ${isPlaceholderData ? 'opacity-60' : ''}`}>
      <div className={`${PANEL} overflow-x-auto`}>
        <table className="min-w-full">
          <thead>
            <tr className="border-b border-gray-200 bg-gray-50">
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
              <th scope="col" className={NUM_HEAD}>
                {t('organization.usage.share', 'Share of spend')}
              </th>
            </tr>
          </thead>
          <tbody>
            {data.members.map((member) => {
              const name = memberName(member);
              return (
                <tr key={member.user_id} className="border-b border-gray-100 last:border-b-0">
                  <td className={TABLE_CELL}>
                    <div className="max-w-[20rem] truncate font-medium text-gray-900">{name}</div>
                    {member.email && member.email !== name ? (
                      <div className="max-w-[20rem] truncate text-[12px] text-gray-500">
                        {member.email}
                      </div>
                    ) : null}
                  </td>
                  <td className={NUM_CELL}>{fmt.number(member.requests)}</td>
                  <td className={NUM_CELL}>{compactNumber(member.total_tokens, fmt)}</td>
                  <td className={`${NUM_CELL} text-gray-900`}>
                    {money.value(member, 'cost', { maximumFractionDigits: 2 })}
                  </td>
                  <td className={NUM_CELL}>{share(moneyAmount(member, 'cost') ?? 0, total)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <Pager
        page={page}
        pageSize={MEMBER_PAGE}
        total={data.total}
        count={data.members.length}
        onPageChange={setPage}
      />
    </div>
  );
}

/**
 * What the organization's members asked for: each request, or the period's
 * totals for each model or each member. No request's content is shown; the
 * gateway does not send it.
 */
export function UsageView() {
  const t = useT();
  const router = useRouter();
  const pathname = usePathname() ?? '';
  const kind = usageKind(useSearchParams()?.get('view') ?? null);
  const { period, setPeriod } = useOrganizationPeriod();
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');
  const [model, setModel] = useState(ALL_MODELS);
  const [status, setStatus] = useState<StatusChoice>('all');
  const ids = useId();

  useEffect(() => {
    const timer = setTimeout(() => setSearch(searchInput.trim()), SEARCH_DELAY_MS);
    return () => clearTimeout(timer);
  }, [searchInput]);

  // The models to narrow the requests to are the ones the period has.
  const models = useOrganizationModelUsage(period, kind === 'requests');
  const modelOptions = [
    { value: ALL_MODELS, label: t('organization.usage.all_models', 'All models') },
    ...(models.data ?? []).map((row) => ({ value: row.model_id, label: row.model_id })),
  ];
  const statusOptions: ReadonlyArray<{ value: StatusChoice; label: string }> = [
    { value: 'all', label: t('organization.usage.status_all', 'All statuses') },
    { value: 'ok', label: t('organization.request.ok', 'OK') },
    { value: 'errors', label: t('organization.usage.errors', 'Errors') },
  ];

  const tab = (key: UsageKind, label: string) => ({
    key,
    label,
    tabId: `${ids}-tab-${key}`,
    panelId: `${ids}-panel-${key}`,
  });
  const tabs = [
    tab('requests', t('organization.usage.requests', 'Requests')),
    tab('models', t('organization.usage.by_model', 'By model')),
    tab('members', t('organization.usage.by_member', 'By member')),
  ];

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <PeriodControl value={period} onChange={setPeriod} />
        {kind !== 'models' ? (
          <input
            type="search"
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
            placeholder={t('organization.usage.search', 'Search by member')}
            aria-label={t('organization.usage.search', 'Search by member')}
            className={`${CONTROL} min-w-[14rem] flex-1`}
          />
        ) : null}
        {kind === 'requests' ? (
          <>
            <Select
              size="sm"
              className="w-52"
              value={model}
              onValueChange={setModel}
              options={modelOptions}
              aria-label={t('organization.requests.model', 'Model')}
            />
            <Select
              size="sm"
              className="w-40"
              value={status}
              onValueChange={setStatus}
              options={statusOptions}
              aria-label={t('organization.requests.status', 'Status')}
            />
          </>
        ) : null}
      </div>

      <AdminSubTabs
        label={t('organization.usage.views', 'Usage views')}
        tabs={tabs}
        active={kind}
        onSelect={(next) =>
          router.replace(next === 'requests' ? pathname : `${pathname}?view=${next}`, {
            scroll: false,
          })
        }
      />

      <div role="tabpanel" id={`${ids}-panel-${kind}`} aria-labelledby={`${ids}-tab-${kind}`}>
        {kind === 'requests' ? (
          <Requests period={period} search={search} model={model} status={status} />
        ) : kind === 'models' ? (
          <ByModel period={period} />
        ) : (
          <ByMember period={period} search={search} />
        )}
      </div>
    </div>
  );
}
