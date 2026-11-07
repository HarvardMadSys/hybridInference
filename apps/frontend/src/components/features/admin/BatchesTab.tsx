'use client';

import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';

import {
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
  AdminSection,
} from '@/components/features/admin/AdminPrimitives';
import {
  BUTTON_SECONDARY,
  CONTROL,
  PANEL,
  PILL,
  ROW_ACTION,
  ROW_ACTION_DANGER,
  TABLE_CELL,
  TABLE_HEAD_CELL,
} from '@/components/features/admin/adminStyles';
import { BatchRequestsPanel } from '@/components/features/admin/BatchRequestsPanel';
import { BATCH_REQUESTS_PATH } from '@/components/features/admin/batchRoutes';
import {
  compactNumber,
  durationText,
  estimatedFinish,
  OutcomeBar,
  sharePercent,
  StatTile,
  summaryOutcomeCounts,
  usd,
} from '@/components/features/admin/batchUsage';
import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import {
  batchCategory,
  CATEGORY_TONE,
  categoryNote,
  outcomeCounts,
  statusLabel,
} from '@/components/features/batches/batchText';
import { usePagedBatches } from '@/components/features/batches/usePagedBatches';
import { Select } from '@/components/ui/Select';
import {
  adminCancelBatch,
  getBatchSummary,
  isCancellable,
  listAllBatches,
  type AdminBatchSummary,
  type BatchStatus,
  type ConsoleBatch,
} from '@/lib/api/batches';
import { translate } from '@/lib/i18n/translate';
import { getErrorMessage } from '@/lib/utils/errors';

/** The statuses the list shows; the filter matches the same ones, finalizing included. */
const STATUSES: (BatchStatus | '')[] = [
  '',
  'validating',
  'in_progress',
  'finalizing',
  'cancelling',
  'completed',
  'failed',
  'expired',
  'cancelled',
];

function when(epoch: number | null | undefined): string {
  return epoch
    ? new Date(epoch * 1000).toLocaleString('en-US', { dateStyle: 'medium', timeStyle: 'short' })
    : '—';
}

function plural(count: number, one: string, many: string): string {
  return `${count.toLocaleString('en-US')} ${count === 1 ? one : many}`;
}

/**
 * Input and output tokens, shortened, with the exact counts on hover, and the
 * part of the input served from a prompt cache. `cached` is null for a batch
 * stored before cache hits were recorded and undefined from an older gateway;
 * `unrecorded` counts such batches within a total, which leave it out.
 */
function Tokens({
  input,
  output,
  cached,
  unrecorded = 0,
}: {
  input: number;
  output: number;
  cached?: number | null;
  unrecorded?: number;
}) {
  return (
    <>
      <span
        title={`${input.toLocaleString('en-US')} input · ${output.toLocaleString('en-US')} output tokens`}
      >
        {compactNumber(input)} in · {compactNumber(output)} out
      </span>
      {cached === null ? (
        <span
          className="block text-[11px] text-gray-400"
          title="This batch ran before cached input was recorded."
        >
          Cached: not recorded
        </span>
      ) : cached !== undefined ? (
        <span
          className="block text-[11px] text-gray-500"
          title={`${cached.toLocaleString('en-US')} input tokens served from a prompt cache, as the upstream reported them; part of the input above.${
            unrecorded > 0
              ? ` ${plural(unrecorded, 'batch', 'batches')} ran before cached input was recorded and ${unrecorded === 1 ? 'is' : 'are'} not included.`
              : ''
          }`}
        >
          {compactNumber(cached)} cached
          {unrecorded === 0 && input > 0 && cached > 0 ? ` (${sharePercent(cached, input)})` : ''}
          {unrecorded > 0 ? ` · ${plural(unrecorded, 'batch', 'batches')} not recorded` : ''}
        </span>
      ) : null}
    </>
  );
}

/** A batch owner by name, with the email and id that identify the account. */
function Owner({
  id,
  name,
  email,
  children,
}: {
  id: string;
  name?: string | null;
  email?: string | null;
  /** Renders the primary label (e.g. as a button); defaults to plain text. */
  children?: (label: string) => React.ReactNode;
}) {
  const label = name || email || id;
  const secondary = [name && email ? email : null, label !== id ? id : null].filter(Boolean);
  return (
    <>
      {children ? children(label) : <span className="text-gray-900">{label}</span>}
      {secondary.map((line) => (
        <span
          key={line}
          className={`block text-[11px] text-gray-500 ${line === id ? 'font-mono' : ''}`}
        >
          {line}
        </span>
      ))}
    </>
  );
}

/** How many attempts a known cost leaves out, because their cost is unknown. */
function Unpriced({ count }: { count: number }) {
  return (
    <span
      className="block text-[11px] text-amber-700"
      title="Attempts whose request was sent but whose result was lost: their cost is unknown and not included."
    >
      + {plural(count, 'unpriced attempt', 'unpriced attempts')}
    </span>
  );
}

/** A known cost, exact on hover, and the attempts it leaves out. */
function Cost({ value, unknown }: { value: number; unknown: number }) {
  return (
    <>
      <span title={`$${value.toFixed(6)}`}>{usd(value)}</span>
      {unknown > 0 && <Unpriced count={unknown} />}
    </>
  );
}

/** A batch's status in words, coloured by its category, with what the category leaves out. */
function StatusBadge({ batch }: { batch: ConsoleBatch }) {
  const note = categoryNote(batch, translate);
  return (
    <>
      <span
        className={`${PILL} whitespace-nowrap ring-1 ring-inset ${CATEGORY_TONE[batchCategory(batch)]}`}
      >
        {statusLabel(batch.status, translate)}
      </span>
      {note && <span className="mt-1 block text-[11px] text-gray-500">{note}</span>}
    </>
  );
}

/** When a running batch should finish at its pace so far, or nothing when there is no pace yet. */
function FinishEstimateText({ batch }: { batch: ConsoleBatch }) {
  const now = Date.now() / 1000;
  const estimate = estimatedFinish(batch, now);
  if (!estimate) return null;
  const { completed, failed, total } = batch.request_counts;
  const done = completed + failed + (batch.cancelled ?? 0);
  const pace = `At the pace so far: ${done.toLocaleString('en-US')} of ${total.toLocaleString(
    'en-US',
  )} requests in ${durationText(now - (batch.started_at ?? now))}.`;
  if (estimate.afterWindow) {
    return (
      <span className="mt-1 block text-[11px] text-amber-700" title={pace}>
        Won&apos;t finish before its window closes ({when(batch.expires_at)}); est.{' '}
        {when(estimate.at)}
      </span>
    );
  }
  return (
    <span className="mt-1 block text-[11px] text-gray-600" title={pace}>
      Est. finish {when(estimate.at)} · in ~{durationText(estimate.in)}
    </span>
  );
}

/** The totals across every user, as headline figures. */
function UsageTiles({ totals }: { totals: AdminBatchSummary['totals'] }) {
  const counts = summaryOutcomeCounts(totals);
  const tokens = totals.prompt_tokens + totals.completion_tokens;
  return (
    <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
      <StatTile
        label="Batches"
        value={totals.batches.toLocaleString('en-US')}
        detail={
          totals.active > 0 ? `${totals.active.toLocaleString('en-US')} running` : 'None running'
        }
      />
      <StatTile
        label="Requests"
        value={compactNumber(counts.total)}
        valueTitle={counts.total.toLocaleString('en-US')}
        detail={
          counts.total > 0 ? `${sharePercent(counts.succeeded, counts.total)} succeeded` : undefined
        }
        className="sm:col-span-2"
      >
        {counts.total > 0 && <OutcomeBar counts={counts} className="mt-2" />}
      </StatTile>
      <StatTile
        label="Tokens"
        value={compactNumber(tokens)}
        valueTitle={tokens.toLocaleString('en-US')}
        detail={
          <Tokens
            input={totals.prompt_tokens}
            output={totals.completion_tokens}
            cached={totals.cached_tokens}
            unrecorded={totals.cache_unrecorded_batches}
          />
        }
      />
      <StatTile
        label="Known cost"
        value={usd(totals.cost_usd)}
        valueTitle={`$${totals.cost_usd.toFixed(6)}`}
        detail={
          totals.unknown_cost_attempts > 0 ? (
            <Unpriced count={totals.unknown_cost_attempts} />
          ) : (
            'Every attempt priced'
          )
        }
      />
    </dl>
  );
}

/**
 * Every user's batches, for administrators: who holds how much batch work, how
 * its requests turned out and what it has cost, and each batch with its
 * progress. An administrator can cancel any batch here (audited); deleting
 * stays with the owner and the API.
 */
export function BatchesTab() {
  const confirmation = useConfirmAction();
  const [summary, setSummary] = useState<AdminBatchSummary | null>(null);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const [userId, setUserId] = useState('');
  const [status, setStatus] = useState<BatchStatus | ''>('');
  /** The batch whose requests are shown under its row. */
  const [openRequests, setOpenRequests] = useState<string | null>(null);
  const owner = userId.trim();
  const {
    batches,
    hasMore,
    loading,
    loadingMore,
    error: listError,
    reload,
    loadMore,
    replace,
  } = usePagedBatches(
    (after) => listAllBatches({ userId: owner, status, after }),
    JSON.stringify([owner, status]),
  );
  const summaryGeneration = useRef(0);

  const loadSummary = useCallback(async () => {
    const mine = ++summaryGeneration.current;
    setSummaryError(null);
    try {
      const totals = await getBatchSummary();
      if (mine === summaryGeneration.current) setSummary(totals);
    } catch (err) {
      if (mine === summaryGeneration.current) setSummaryError(getErrorMessage(err));
    }
  }, []);

  useEffect(() => {
    void loadSummary();
  }, [loadSummary]);

  const error = summaryError ?? listError;
  const ownerUsage = summary?.users.find((user) => user.user_id === owner);
  const ownerLabel = ownerUsage?.user_name || ownerUsage?.email || owner;

  function refresh() {
    void loadSummary();
    void reload();
  }

  function cancel(batch: ConsoleBatch) {
    confirmation.request({
      title: 'Cancel batch',
      body: (
        <p>
          Cancel <code>{batch.id}</code> of <code>{batch.user_id}</code>? Requests that have not
          started will not run; running ones finish.
        </p>
      ),
      confirmLabel: 'Cancel batch',
      busyLabel: 'Cancelling…',
      cancelLabel: 'Keep running',
      tone: 'danger',
      run: async () => {
        replace(await adminCancelBatch(batch.id));
      },
    });
  }

  return (
    <div className="space-y-8">
      {confirmation.dialog}
      <AdminSection
        title="Batch usage"
        description="Totals across every stored batch, then per owner. Known cost leaves out attempts whose cost is unknown. Select a user to list only their batches below."
        actions={
          <button type="button" className={BUTTON_SECONDARY} onClick={refresh}>
            Refresh
          </button>
        }
      >
        {error && <AdminNotice tone="error">{error}</AdminNotice>}
        {!summary && !summaryError ? (
          <AdminLoading label="Loading batch usage" />
        ) : !summary || summary.users.length === 0 ? (
          <AdminEmptyState title="No batches">No user has a stored batch.</AdminEmptyState>
        ) : (
          <div className="space-y-4">
            <UsageTiles totals={summary.totals} />
            <div className={`${PANEL} relative overflow-x-auto`}>
              <table className="min-w-full divide-y divide-gray-200">
                <thead>
                  <tr>
                    <th className={TABLE_HEAD_CELL}>User</th>
                    <th className={TABLE_HEAD_CELL}>Batches</th>
                    <th className={TABLE_HEAD_CELL}>Requests</th>
                    <th className={`${TABLE_HEAD_CELL} min-w-[260px]`}>Outcome</th>
                    <th className={TABLE_HEAD_CELL}>Tokens</th>
                    <th className={TABLE_HEAD_CELL}>Known cost</th>
                    <th className={TABLE_HEAD_CELL}>Last batch</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {summary.users.map((user) => {
                    const counts = summaryOutcomeCounts(user);
                    const selected = owner === user.user_id;
                    return (
                      <tr
                        key={user.user_id}
                        className={selected ? 'bg-blue-50/60' : undefined}
                        data-testid={`usage-${user.user_id}`}
                      >
                        <td className={TABLE_CELL}>
                          <Owner id={user.user_id} name={user.user_name} email={user.email}>
                            {(label) => (
                              <button
                                type="button"
                                className={`text-left text-blue-700 underline-offset-2 hover:underline ${
                                  label === user.user_id ? 'font-mono text-[12px]' : 'font-medium'
                                }`}
                                title={
                                  selected
                                    ? 'List every user’s batches'
                                    : 'List only this user’s batches'
                                }
                                aria-pressed={selected}
                                onClick={() => setUserId(selected ? '' : user.user_id)}
                              >
                                {label}
                              </button>
                            )}
                          </Owner>
                        </td>
                        <td className={`${TABLE_CELL} whitespace-nowrap`}>
                          {user.batches.toLocaleString('en-US')}
                          {user.active > 0 && (
                            <span className="block text-[11px] text-blue-700">
                              {user.active.toLocaleString('en-US')} running
                            </span>
                          )}
                        </td>
                        <td className={`${TABLE_CELL} whitespace-nowrap`}>
                          <span title={counts.total.toLocaleString('en-US')}>
                            {compactNumber(counts.total)}
                          </span>
                          {counts.total > 0 && (
                            <span className="block text-[11px] text-gray-500">
                              {sharePercent(counts.succeeded, counts.total)} succeeded
                            </span>
                          )}
                        </td>
                        <td className={TABLE_CELL}>
                          <OutcomeBar counts={counts} />
                        </td>
                        <td className={`${TABLE_CELL} whitespace-nowrap`}>
                          <Tokens
                            input={user.prompt_tokens}
                            output={user.completion_tokens}
                            cached={user.cached_tokens}
                            unrecorded={user.cache_unrecorded_batches}
                          />
                        </td>
                        <td className={`${TABLE_CELL} whitespace-nowrap`}>
                          <Cost value={user.cost_usd} unknown={user.unknown_cost_attempts} />
                        </td>
                        <td className={`${TABLE_CELL} whitespace-nowrap`}>
                          {when(user.last_created_at)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </AdminSection>

      <AdminSection
        title={owner ? `Batches of ${ownerLabel}` : 'All batches'}
        description="Newest first. Filter by owner or by status."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <input
              aria-label="Owner user id"
              placeholder="Owner user id"
              className={CONTROL}
              value={userId}
              onChange={(event) => setUserId(event.target.value)}
            />
            {userId && (
              <button type="button" className={ROW_ACTION} onClick={() => setUserId('')}>
                Clear
              </button>
            )}
            <Select<BatchStatus | ''>
              aria-label="Status"
              value={status}
              onValueChange={setStatus}
              options={STATUSES.map((value) => ({
                value,
                label: value ? statusLabel(value, translate) : 'Any status',
              }))}
              className="w-36"
            />
          </div>
        }
      >
        {loading && batches.length === 0 ? (
          <AdminLoading label="Loading batches" />
        ) : batches.length === 0 ? (
          <AdminEmptyState title="No batches">No batch matches these filters.</AdminEmptyState>
        ) : (
          <div className={`${PANEL} relative overflow-x-auto`}>
            <table className="min-w-full divide-y divide-gray-200">
              <thead>
                <tr>
                  <th className={TABLE_HEAD_CELL}>Batch</th>
                  <th className={TABLE_HEAD_CELL}>Owner</th>
                  <th className={TABLE_HEAD_CELL}>Target</th>
                  <th className={TABLE_HEAD_CELL}>Status</th>
                  <th className={`${TABLE_HEAD_CELL} min-w-[220px]`}>Requests</th>
                  <th className={TABLE_HEAD_CELL}>Tokens</th>
                  <th className={TABLE_HEAD_CELL}>Known cost</th>
                  <th className={TABLE_HEAD_CELL}>Created</th>
                  <th className={TABLE_HEAD_CELL}>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {batches.map((batch) => (
                  <Fragment key={batch.id}>
                    <tr data-testid={`batch-${batch.id}`}>
                      <td className={TABLE_CELL}>
                        {batch.name && (
                          <span className="block max-w-[16rem] truncate" title={batch.name}>
                            {batch.name}
                          </span>
                        )}
                        <span className="font-mono text-[12px] text-gray-500">{batch.id}</span>
                      </td>
                      <td className={TABLE_CELL}>
                        <Owner
                          id={batch.user_id ?? ''}
                          name={batch.user_name}
                          email={batch.user_email}
                        />
                      </td>
                      <td className={TABLE_CELL}>
                        {batch.endpoint ?? batch.target}
                        <span className="block text-[11px] text-gray-500">
                          {batch.input === 'file' ? 'JSONL file' : 'Direct requests'}
                          {batch.models.length ? ` · ${batch.models.join(', ')}` : ''}
                        </span>
                      </td>
                      <td className={TABLE_CELL}>
                        <StatusBadge batch={batch} />
                        <FinishEstimateText batch={batch} />
                      </td>
                      <td className={TABLE_CELL}>
                        <OutcomeBar counts={outcomeCounts(batch)} />
                      </td>
                      <td className={`${TABLE_CELL} whitespace-nowrap`}>
                        <Tokens
                          input={batch.usage.input_tokens}
                          output={batch.usage.output_tokens}
                          cached={batch.usage.cached_input_tokens}
                        />
                      </td>
                      <td className={`${TABLE_CELL} whitespace-nowrap`}>
                        <Cost value={batch.cost_usd} unknown={batch.cost_unknown_attempts} />
                      </td>
                      <td className={`${TABLE_CELL} whitespace-nowrap`}>
                        {when(batch.created_at)}
                      </td>
                      <td className={`${TABLE_CELL} whitespace-nowrap`}>
                        <button
                          type="button"
                          className={ROW_ACTION}
                          aria-expanded={openRequests === batch.id}
                          aria-controls={`requests-row-${batch.id}`}
                          onClick={() =>
                            setOpenRequests((open) => (open === batch.id ? null : batch.id))
                          }
                        >
                          {openRequests === batch.id ? 'Hide prompts' : 'Prompts'}
                        </button>
                        <Link
                          href={`${BATCH_REQUESTS_PATH}?batch=${encodeURIComponent(batch.id)}`}
                          prefetch={false}
                          className={ROW_ACTION}
                          title="This batch's requests in the request log: each attempt with its route, latency, tokens and cost"
                        >
                          Requests
                        </Link>
                        {isCancellable(batch) && (
                          <button
                            type="button"
                            className={ROW_ACTION_DANGER}
                            disabled={confirmation.busy}
                            onClick={() => cancel(batch)}
                          >
                            Cancel
                          </button>
                        )}
                      </td>
                    </tr>
                    {openRequests === batch.id && (
                      <tr id={`requests-row-${batch.id}`}>
                        <td colSpan={9} className="bg-gray-50 px-4 py-3">
                          <BatchRequestsPanel batchId={batch.id} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {hasMore && !loading && (
          <div className="mt-3">
            <button
              type="button"
              className={BUTTON_SECONDARY}
              disabled={loading || loadingMore}
              aria-busy={loadingMore || undefined}
              onClick={() => void loadMore()}
            >
              Show more
            </button>
          </div>
        )}
      </AdminSection>
    </div>
  );
}
