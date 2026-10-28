'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';

import { Button } from '@/components/ui/Button';
import { Select } from '@/components/ui/Select';
import { useT } from '@/components/providers/useT';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import {
  cancelMyBatch,
  deleteMyBatch,
  downloadMyResults,
  isCancellable,
  listMyBatches,
  type BatchCapabilities,
  BATCH_CATEGORIES,
  BatchCategoryFilterUnavailable,
  type BatchCategory,
  type ConsoleBatch,
  type ResultKind,
} from '@/lib/api/batches';
import { useBatchCapabilities } from '@/lib/hooks/queries/useBatchCapabilities';
import { getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { roleLabel } from '@/lib/utils/roleLabel';

import { categoryLabel, formatTime, outcomeCounts, pauseText, targetLabel } from './batchText';
import { BatchCategoryBadge } from './BatchCategoryBadge';
import { usePagedBatches } from './usePagedBatches';
import { WebhookSettings } from './WebhookSettings';

export { inputLabel, progress, statusLabel, targetLabel } from './batchText';

function BatchRow({
  batch,
  busy,
  onCancel,
  onDelete,
  onDownload,
}: {
  batch: ConsoleBatch;
  busy: boolean;
  onCancel: (batch: ConsoleBatch) => void;
  onDelete: (batch: ConsoleBatch) => void;
  onDownload: (batch: ConsoleBatch, kind: ResultKind) => void;
}) {
  const t = useT();
  const counts = outcomeCounts(batch);
  const paused = pauseText(batch, t);
  const name = batch.name || batch.id;
  const outcome = [
    counts.total > 0 &&
      fill(t('batches.row.success_fraction', '{succeeded} / {total} succeeded'), {
        succeeded: counts.succeeded.toLocaleString(),
        total: counts.total.toLocaleString(),
      }),
    counts.failed > 0 &&
      fill(t('batches.row.failed_nonzero', '{count} failed'), {
        count: counts.failed.toLocaleString(),
      }),
    counts.expired > 0 &&
      fill(t('batches.row.expired_nonzero', '{count} expired unrun'), {
        count: counts.expired.toLocaleString(),
      }),
    counts.cancelled > 0 &&
      fill(t('batches.row.cancelled_nonzero', '{count} cancelled'), {
        count: counts.cancelled.toLocaleString(),
      }),
    counts.unfinished > 0 &&
      fill(t('batches.row.unfinished_nonzero', '{count} unfinished'), {
        count: counts.unfinished.toLocaleString(),
      }),
  ]
    .filter(Boolean)
    .join(' · ');
  const primaryDownload: ResultKind | null = !batch.results_available
    ? null
    : batch.input !== 'file'
      ? 'all'
      : batch.error_file_id && counts.succeeded === 0
        ? 'error'
        : batch.output_file_id
          ? 'output'
          : batch.error_file_id
            ? 'error'
            : null;

  return (
    <li className="py-5" data-testid={`batch-${batch.id}`}>
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
            <Link
              href={`/dashboard/batches/${encodeURIComponent(batch.id)}`}
              className="min-w-0 max-w-full truncate text-sm font-semibold text-gray-900 hover:text-blue-700 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
              title={name}
            >
              {name}
            </Link>
            <BatchCategoryBadge batch={batch} />
          </div>
          <p className="mt-1.5 break-words text-sm text-gray-600">
            {targetLabel(batch, t)}
            {batch.models.length ? ` · ${batch.models.join(', ')}` : ''}
          </p>
          {outcome && <p className="mt-2 text-sm text-gray-700">{outcome}</p>}
          {batch.created_at && (
            <p className="mt-1 text-xs text-gray-500">
              {fill(t('batches.row.created_compact', 'Created {time}'), {
                time: formatTime(batch.created_at),
              })}
            </p>
          )}
          {paused && (
            <p role="note" className="mt-2 text-xs text-amber-800">
              ⚠ {paused}
            </p>
          )}
          {batch.cost_unknown_attempts > 0 && (
            <p role="note" className="mt-2 text-xs text-amber-800">
              {fill(t('batches.row.unknown_cost_visible', 'Cost is unknown for {count} attempts'), {
                count: batch.cost_unknown_attempts.toLocaleString(),
              })}
            </p>
          )}
          {batch.validation_error?.data?.length ? (
            <p className="mt-2 break-words text-xs text-red-700">
              {batch.validation_error.data.slice(0, 1).map((problem) =>
                problem.line
                  ? fill(t('batches.row.line_problem', 'Line {line}: {message}'), {
                      line: String(problem.line),
                      message: problem.message ?? '',
                    })
                  : (problem.message ?? ''),
              )}
            </p>
          ) : null}
          {batch.error?.message ? (
            <p className="mt-2 break-words text-xs text-red-700">{batch.error.message}</p>
          ) : null}
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {primaryDownload && (
            <Button
              size="sm"
              variant="secondary"
              disabled={busy}
              onClick={() => {
                if (primaryDownload) onDownload(batch, primaryDownload);
              }}
            >
              {primaryDownload === 'all'
                ? t('batches.action.download', 'Download results')
                : primaryDownload === 'output'
                  ? t('batches.action.download_output', 'Download outputs')
                  : t('batches.action.download_errors', 'Download errors')}
            </Button>
          )}
          {isCancellable(batch) && (
            <Button size="sm" variant="secondary" disabled={busy} onClick={() => onCancel(batch)}>
              {t('batches.action.cancel', 'Cancel')}
            </Button>
          )}
          <Link
            href={`/dashboard/batches/${encodeURIComponent(batch.id)}`}
            className="inline-flex h-9 items-center rounded-md px-3 text-sm font-medium text-blue-700 hover:bg-blue-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
          >
            {t('batches.action.details', 'Details')}
          </Link>
          <details
            className="group relative"
            onKeyDown={(event) => {
              if (event.key === 'Escape' && event.currentTarget.open) {
                event.currentTarget.open = false;
                event.currentTarget.querySelector('summary')?.focus();
                event.stopPropagation();
              }
            }}
          >
            <summary
              aria-label={fill(t('batches.action.more_for', 'More actions for {name}'), { name })}
              className="flex h-9 cursor-pointer list-none items-center rounded-md px-3 text-sm font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 [&::-webkit-details-marker]:hidden"
            >
              {t('batches.action.more', 'More')}{' '}
              <span aria-hidden="true" className="ml-1">
                ▾
              </span>
            </summary>
            <div className="absolute right-0 z-20 mt-1 w-44 rounded-lg bg-white p-1 shadow-lg ring-1 ring-gray-200">
              {batch.results_available && batch.output_file_id && primaryDownload !== 'output' && (
                <button
                  type="button"
                  disabled={busy}
                  className="block w-full rounded-md px-3 py-2 text-left text-sm text-gray-700 hover:bg-gray-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 disabled:opacity-50"
                  onClick={(event) => {
                    const menu = event.currentTarget.closest('details');
                    menu?.removeAttribute('open');
                    menu?.querySelector('summary')?.focus();
                    onDownload(batch, 'output');
                  }}
                >
                  {t('batches.action.download_output', 'Download outputs')}
                </button>
              )}
              {batch.results_available && batch.error_file_id && primaryDownload !== 'error' && (
                <button
                  type="button"
                  disabled={busy}
                  className="block w-full rounded-md px-3 py-2 text-left text-sm text-gray-700 hover:bg-gray-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 disabled:opacity-50"
                  onClick={(event) => {
                    const menu = event.currentTarget.closest('details');
                    menu?.removeAttribute('open');
                    menu?.querySelector('summary')?.focus();
                    onDownload(batch, 'error');
                  }}
                >
                  {t('batches.action.download_errors', 'Download errors')}
                </button>
              )}
              <button
                type="button"
                disabled={busy}
                className="block w-full rounded-md px-3 py-2 text-left text-sm text-red-700 hover:bg-red-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-red-600 disabled:opacity-50"
                onClick={(event) => {
                  const menu = event.currentTarget.closest('details');
                  menu?.removeAttribute('open');
                  menu?.querySelector('summary')?.focus();
                  onDelete(batch);
                }}
              >
                {t('batches.action.delete', 'Delete')}
              </button>
            </div>
          </details>
        </div>
      </div>
    </li>
  );
}

function PageHeader({ subtitle, children }: { subtitle?: string; children?: React.ReactNode }) {
  const t = useT();
  return (
    <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <Link href="/dashboard" className="text-sm text-gray-500 hover:text-gray-900">
          ← {t('batches.back', 'Dashboard')}
        </Link>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight text-gray-900">
          {t('batches.title', 'Batches')}
        </h1>
        {subtitle && <p className="mt-1 text-sm text-gray-500">{subtitle}</p>}
      </div>
      {children}
    </div>
  );
}

/** Batch is switched off, or not ready on this gateway: nothing to load or manage. */
function Unavailable({ status }: { status: BatchCapabilities['status'] }) {
  const t = useT();
  return (
    <section
      role="status"
      className="rounded-xl bg-white p-6 shadow-sm ring-1 ring-gray-200"
      data-testid="batches-unavailable"
    >
      <h2 className="text-base font-semibold text-gray-900">
        {t('batches.unavailable.title', 'Batch is not available')}
      </h2>
      <p className="mt-1 text-sm text-gray-500">
        {status === 'disabled'
          ? t(
              'batches.unavailable.disabled',
              'Batch inference is turned off on this gateway. Batches you submitted before are kept and show here again when it is turned back on.',
            )
          : t(
              'batches.unavailable.not_ready',
              'Batch inference is temporarily unavailable on this gateway. Try again in a moment.',
            )}
      </p>
    </section>
  );
}

/**
 * The signed-in user's batches and webhook, or why Batch is not offered.
 *
 * Whether the user may submit comes from the gateway (their current role
 * against `BATCH_MIN_ROLE`); one who may not still manages what they have.
 * When that cannot be looked up, the page works as before and the gateway's
 * own checks answer each action.
 */
export function BatchesView() {
  const t = useT();
  const { capabilities, loading, error } = useBatchCapabilities();
  if (loading) {
    return (
      <div className="space-y-6">
        <PageHeader />
        <p className="text-sm text-gray-500">{t('batches.loading', 'Loading batches…')}</p>
      </div>
    );
  }
  if (capabilities && capabilities.status !== 'enabled') {
    return (
      <div className="space-y-6">
        <PageHeader />
        <Unavailable status={capabilities.status} />
      </div>
    );
  }
  return <BatchesManager capabilities={capabilities} capabilitiesError={error} />;
}

/**
 * The signed-in user's batches, every target alike, with what each has done and
 * cost so far, the actions an owner has (cancel, delete, download results) and
 * their webhook settings. A user whose role may submit also creates batches
 * here (`/dashboard/batches/new`), and filters the list by name, id and status.
 */
function BatchesManager({
  capabilities,
  capabilitiesError,
}: {
  capabilities: BatchCapabilities | null;
  capabilitiesError: string | null;
}) {
  const t = useT();
  const toast = useAccountToast();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [appliedQuery, setAppliedQuery] = useState('');
  const [category, setCategory] = useState<BatchCategory | ''>('');
  useEffect(() => {
    // Search as the user types, without a request per keystroke.
    const timer = setTimeout(() => setAppliedQuery(query.trim()), 300);
    return () => clearTimeout(timer);
  }, [query]);
  const { batches, hasMore, loading, loadingMore, error, reload, loadMore, replace, remove } =
    usePagedBatches(
      (after) =>
        listMyBatches(after, 20, { category, q: appliedQuery }).catch((err: unknown) => {
          // A gateway not yet upgraded answered: its page is not filtered.
          if (err instanceof BatchCategoryFilterUnavailable) {
            throw new Error(
              t(
                'batches.filter.unavailable',
                'Filtering by status is not available right now while the service is being updated. Try again in a moment, or show all batches.',
              ),
            );
          }
          throw err;
        }),
      `mine:${category}:${appliedQuery}`,
    );
  // Only a known "no": without an answer the gateway decides each action.
  const cannotSubmit = capabilities?.role_may_submit === false;
  const filtered = Boolean(category || appliedQuery);
  async function handleCancel(batch: ConsoleBatch) {
    const message = t(
      'batches.confirm.cancel',
      'Cancel this batch? Requests that have not started will not run; running ones finish.',
    );
    if (!window.confirm(message)) return;
    setBusyId(batch.id);
    try {
      replace(await cancelMyBatch(batch.id));
      toast.success(t('batches.toast.cancelled', 'Batch cancelled.'));
    } catch (err) {
      toast.error(getErrorMessage(err));
    } finally {
      setBusyId(null);
    }
  }

  async function handleDelete(batch: ConsoleBatch) {
    const message = t(
      'batches.confirm.delete',
      'Delete this batch and its results? This cannot be undone.',
    );
    if (!window.confirm(message)) return;
    setBusyId(batch.id);
    try {
      await deleteMyBatch(batch.id);
      remove(batch.id);
      toast.success(t('batches.toast.deleted', 'Batch deleted.'));
    } catch (err) {
      toast.error(getErrorMessage(err));
    } finally {
      setBusyId(null);
    }
  }

  async function handleDownload(batch: ConsoleBatch, kind: ResultKind) {
    setBusyId(batch.id);
    try {
      await downloadMyResults(batch.id, kind);
    } catch (err) {
      toast.error(getErrorMessage(err));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader subtitle={t('batches.subtitle_create', 'Manage batch tasks and results.')}>
        <div className="flex flex-wrap gap-2">
          <Button variant="secondary" onClick={() => void reload()} disabled={loading}>
            {t('batches.refresh', 'Refresh')}
          </Button>
          {!cannotSubmit && (
            <Link
              href="/dashboard/batches/new"
              className="inline-flex items-center rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-blue-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2"
            >
              {t('batches.create.button', 'Create batch')}
            </Link>
          )}
        </div>
      </PageHeader>

      {cannotSubmit && capabilities && (
        <div
          role="note"
          className="rounded-lg bg-amber-50 p-4 text-sm text-amber-900 ring-1 ring-inset ring-amber-200"
        >
          {fill(
            t(
              'batches.notice.cannot_submit',
              'Your plan cannot submit new batches: that needs the {role} plan or higher. Your existing batches stay manageable here: download their results, cancel or delete them, and view or remove your webhook.',
            ),
            { role: roleLabel(capabilities.min_role, t) },
          )}
        </div>
      )}
      {capabilitiesError && !capabilities && (
        <p role="alert" className="text-sm text-amber-800">
          {fill(
            t(
              'batches.notice.capabilities_error',
              'Could not check what Batch offers your account ({error}). Your batches are listed below.',
            ),
            { error: capabilitiesError },
          )}
        </p>
      )}

      <section className="rounded-xl bg-white p-6 shadow-sm ring-1 ring-gray-200">
        <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-center">
          <input
            type="search"
            aria-label={t('batches.search.label', 'Search by name or batch ID')}
            placeholder={t('batches.search.placeholder', 'Search by name or batch ID')}
            className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20 sm:max-w-xs"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <Select<BatchCategory | ''>
            aria-label={t('batches.filter.status', 'Status')}
            value={category}
            onValueChange={setCategory}
            options={[
              { value: '', label: t('batches.filter.all', 'All') },
              ...BATCH_CATEGORIES.map((value) => ({ value, label: categoryLabel(value, t) })),
            ]}
            className="sm:w-48"
          />
        </div>
        {error && (
          <p role="alert" className="mb-3 text-sm text-red-700">
            {error}
          </p>
        )}
        {loading ? (
          <p className="text-sm text-gray-500">{t('batches.loading', 'Loading batches…')}</p>
        ) : batches.length === 0 && !error ? (
          <p className="text-sm text-gray-500">
            {filtered
              ? t('batches.empty_filtered', 'No batch matches this search.')
              : cannotSubmit
                ? t('batches.empty_existing', 'You have no batches.')
                : t(
                    'batches.empty_create',
                    'You have no batches. Create one here, or submit one through the API.',
                  )}
          </p>
        ) : (
          <ul className="divide-y divide-gray-100">
            {batches.map((batch) => (
              <BatchRow
                key={batch.id}
                batch={batch}
                busy={busyId === batch.id}
                onCancel={handleCancel}
                onDelete={handleDelete}
                onDownload={handleDownload}
              />
            ))}
          </ul>
        )}
        {hasMore && !loading && (
          <div className="mt-4">
            <Button
              variant="secondary"
              disabled={loadingMore}
              aria-busy={loadingMore || undefined}
              onClick={() => void loadMore()}
            >
              {t('batches.more', 'Show more')}
            </Button>
          </div>
        )}
      </section>

      <WebhookSettings canEdit={!cannotSubmit} />
    </div>
  );
}
