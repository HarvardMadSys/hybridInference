'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

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
  ROW_ACTION_DANGER,
  TABLE_CELL,
  TABLE_HEAD_CELL,
} from '@/components/features/admin/adminStyles';
import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import { usePagedBatches } from '@/components/features/batches/usePagedBatches';
import { Select } from '@/components/ui/Select';
import {
  adminCancelBatch,
  getBatchSummary,
  isCancellable,
  listAllBatches,
  type AdminBatchSummary,
  type BatchSurface,
  type ConsoleBatch,
} from '@/lib/api/batches';
import { getErrorMessage } from '@/lib/utils/errors';

const SURFACES: { value: BatchSurface | ''; label: string }[] = [
  { value: '', label: 'All APIs' },
  { value: 'openai_inline', label: 'OpenAI inline' },
  { value: 'openai_file', label: 'OpenAI file' },
  { value: 'anthropic', label: 'Anthropic' },
];

const STATUSES = [
  '',
  'validating',
  'in_progress',
  'cancelling',
  'completed',
  'failed',
  'expired',
  'cancelled',
];

function usd(value: number): string {
  return `$${value.toFixed(4)}`;
}

function when(epoch: number | null): string {
  return epoch ? new Date(epoch * 1000).toLocaleString('en-US') : '—';
}

/** Done / total of a batch, whichever protocol's tallies it has. */
function progressText(batch: ConsoleBatch): string {
  const counts = batch.request_counts;
  if (batch.api_surface === 'anthropic') {
    const total = Object.values(counts).reduce((sum, n) => sum + n, 0);
    return `${total - (counts.processing ?? 0)}/${total}`;
  }
  return `${(counts.completed ?? 0) + (counts.failed ?? 0) + batch.cancelled}/${counts.total ?? 0}`;
}

/**
 * Every user's batches, for administrators: who holds how much batch work and
 * what it has cost, and each batch with its progress. An administrator can
 * cancel any batch here (audited); deleting stays with the owner and the API.
 */
export function BatchesTab() {
  const confirmation = useConfirmAction();
  const [summary, setSummary] = useState<AdminBatchSummary | null>(null);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const [userId, setUserId] = useState('');
  const [status, setStatus] = useState('');
  const [surface, setSurface] = useState<BatchSurface | ''>('');
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
    (after) => listAllBatches({ userId: owner, status, surface, after }),
    JSON.stringify([owner, status, surface]),
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

  const totals = summary?.totals;
  return (
    <div className="space-y-8">
      {confirmation.dialog}
      <AdminSection
        title="Batch usage by user"
        description="Every stored batch, by owner: how many are unfinished, the requests they hold, tokens and the cost known so far. Attempts whose cost is unknown are counted, not priced."
      >
        {error && <AdminNotice tone="error">{error}</AdminNotice>}
        {!summary && !summaryError ? (
          <AdminLoading label="Loading batch usage" />
        ) : !summary || summary.users.length === 0 ? (
          <AdminEmptyState title="No batches">No user has a stored batch.</AdminEmptyState>
        ) : (
          <div className={`${PANEL} overflow-x-auto`}>
            <table className="min-w-full divide-y divide-gray-200">
              <thead>
                <tr>
                  <th className={TABLE_HEAD_CELL}>User</th>
                  <th className={TABLE_HEAD_CELL}>Batches</th>
                  <th className={TABLE_HEAD_CELL}>Active</th>
                  <th className={TABLE_HEAD_CELL}>Requests</th>
                  <th className={TABLE_HEAD_CELL}>Tokens in / out</th>
                  <th className={TABLE_HEAD_CELL}>Known cost</th>
                  <th className={TABLE_HEAD_CELL}>Unknown-cost attempts</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {summary.users.map((user) => (
                  <tr key={user.user_id}>
                    <td className={TABLE_CELL}>
                      <button
                        type="button"
                        className="font-mono text-[12px] underline-offset-2 hover:underline"
                        onClick={() => setUserId(user.user_id)}
                      >
                        {user.user_id}
                      </button>
                    </td>
                    <td className={TABLE_CELL}>{user.batches}</td>
                    <td className={TABLE_CELL}>{user.active}</td>
                    <td className={TABLE_CELL}>
                      {user.completed + user.failed}/{user.requests}
                    </td>
                    <td className={TABLE_CELL}>
                      {user.prompt_tokens.toLocaleString()} /{' '}
                      {user.completion_tokens.toLocaleString()}
                    </td>
                    <td className={TABLE_CELL}>{usd(user.cost_usd)}</td>
                    <td className={TABLE_CELL}>{user.unknown_cost_attempts}</td>
                  </tr>
                ))}
              </tbody>
              {totals && (
                <tfoot>
                  <tr className="font-medium">
                    <td className={TABLE_CELL}>All users</td>
                    <td className={TABLE_CELL}>{totals.batches}</td>
                    <td className={TABLE_CELL}>{totals.active}</td>
                    <td className={TABLE_CELL}>
                      {totals.completed + totals.failed}/{totals.requests}
                    </td>
                    <td className={TABLE_CELL}>
                      {totals.prompt_tokens.toLocaleString()} /{' '}
                      {totals.completion_tokens.toLocaleString()}
                    </td>
                    <td className={TABLE_CELL}>{usd(totals.cost_usd)}</td>
                    <td className={TABLE_CELL}>{totals.unknown_cost_attempts}</td>
                  </tr>
                </tfoot>
              )}
            </table>
          </div>
        )}
      </AdminSection>

      <AdminSection
        title="All batches"
        description="Newest first. Filter by owner, internal status or API."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" className={BUTTON_SECONDARY} onClick={refresh}>
              Refresh
            </button>
            <input
              aria-label="Owner user id"
              placeholder="Owner user id"
              className={CONTROL}
              value={userId}
              onChange={(event) => setUserId(event.target.value)}
            />
            <Select
              aria-label="Status"
              value={status}
              onValueChange={setStatus}
              options={STATUSES.map((value) => ({ value, label: value || 'Any status' }))}
              className="w-36"
            />
            <Select<BatchSurface | ''>
              aria-label="API"
              value={surface}
              onValueChange={setSurface}
              options={SURFACES}
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
          <div className={`${PANEL} overflow-x-auto`}>
            <table className="min-w-full divide-y divide-gray-200">
              <thead>
                <tr>
                  <th className={TABLE_HEAD_CELL}>Batch</th>
                  <th className={TABLE_HEAD_CELL}>Owner</th>
                  <th className={TABLE_HEAD_CELL}>API / target</th>
                  <th className={TABLE_HEAD_CELL}>Status</th>
                  <th className={TABLE_HEAD_CELL}>Done</th>
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
                  <tr key={batch.id}>
                    <td className={`${TABLE_CELL} font-mono text-[12px]`}>{batch.id}</td>
                    <td className={`${TABLE_CELL} font-mono text-[12px]`}>{batch.user_id}</td>
                    <td className={TABLE_CELL}>
                      {batch.api_surface} / {batch.target}
                    </td>
                    <td className={TABLE_CELL}>
                      <span className={`${PILL} bg-gray-100 text-gray-700`}>{batch.status}</span>
                    </td>
                    <td className={TABLE_CELL}>{progressText(batch)}</td>
                    <td className={TABLE_CELL}>{batch.usage.total_tokens.toLocaleString()}</td>
                    <td className={TABLE_CELL}>
                      {usd(batch.cost_usd)}
                      {batch.cost_unknown_attempts > 0
                        ? ` (+${batch.cost_unknown_attempts} unknown)`
                        : ''}
                    </td>
                    <td className={TABLE_CELL}>{when(batch.created_at)}</td>
                    <td className={TABLE_CELL}>
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
