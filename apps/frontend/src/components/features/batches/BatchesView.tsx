'use client';

import { useState } from 'react';
import Link from 'next/link';

import { Button } from '@/components/ui/Button';
import { useT, type Translate } from '@/components/providers/useT';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import {
  cancelMyBatch,
  deleteMyBatch,
  downloadMyResults,
  isCancellable,
  listMyBatches,
  type BatchCapabilities,
  type BatchStatus,
  type ConsoleBatch,
  type ResultKind,
} from '@/lib/api/batches';
import { useBatchCapabilities } from '@/lib/hooks/queries/useBatchCapabilities';
import { getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { roleLabel } from '@/lib/utils/roleLabel';

import { usePagedBatches } from './usePagedBatches';
import { WebhookSettings } from './WebhookSettings';

const STATUS_TONE: Partial<Record<BatchStatus, string>> = {
  completed: 'bg-green-50 text-green-700 ring-green-200',
  failed: 'bg-red-50 text-red-700 ring-red-200',
  expired: 'bg-amber-50 text-amber-800 ring-amber-200',
  cancelled: 'bg-gray-100 text-gray-700 ring-gray-200',
  cancelling: 'bg-gray-100 text-gray-700 ring-gray-200',
};

/** What a batch runs, as the list names it: the target, whatever API shape it uses. */
export function targetLabel(batch: ConsoleBatch, t: Translate): string {
  switch (batch.target) {
    case 'chat':
      return t('batches.target.chat', 'Chat completions');
    case 'embeddings':
      return t('batches.target.embeddings', 'Embeddings');
    case 'messages':
      return t('batches.target.messages', 'Messages');
    case 'responses':
      return t('batches.target.responses', 'Responses');
    default:
      return batch.endpoint ?? t('batches.target.unknown', 'Batch');
  }
}

/** How the batch's requests arrived: an input method of the one batch API, not an API. */
export function inputLabel(batch: ConsoleBatch, t: Translate): string {
  return batch.input === 'file'
    ? t('batches.input.file', 'JSONL file')
    : t('batches.input.inline', 'Direct requests');
}

/** A batch's status in words; every batch uses the same ones. */
export function statusLabel(status: BatchStatus, t: Translate): string {
  const labels: Record<BatchStatus, string> = {
    validating: t('batches.status.validating', 'Validating'),
    in_progress: t('batches.status.in_progress', 'In progress'),
    finalizing: t('batches.status.finalizing', 'Finalizing'),
    completed: t('batches.status.completed', 'Completed'),
    failed: t('batches.status.failed', 'Failed'),
    expired: t('batches.status.expired', 'Expired'),
    cancelling: t('batches.status.cancelling', 'Cancelling'),
    cancelled: t('batches.status.cancelled', 'Cancelled'),
  };
  return labels[status] ?? status;
}

function formatTime(epoch: number | null): string {
  if (!epoch) return '—';
  return new Date(epoch * 1000).toLocaleString();
}

/** Done / total: completed, failed and cancelled requests are done. */
export function progress(batch: ConsoleBatch): { done: number; total: number; failed: number } {
  const counts = batch.request_counts;
  return {
    done: counts.completed + counts.failed + batch.cancelled,
    total: counts.total,
    failed: counts.failed,
  };
}

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
  const { done, total, failed } = progress(batch);
  const tone = STATUS_TONE[batch.status] ?? 'bg-blue-50 text-blue-700 ring-blue-200';
  return (
    <li className="py-4" data-testid={`batch-${batch.id}`}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <code className="truncate text-sm font-medium text-gray-900">{batch.id}</code>
            <span
              className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${tone}`}
            >
              {statusLabel(batch.status, t)}
            </span>
          </div>
          <p className="mt-1 text-sm text-gray-700">
            <span className="font-medium">{targetLabel(batch, t)}</span>
            {batch.models.length ? ` · ${batch.models.join(', ')}` : ''}
          </p>
          <p className="mt-0.5 text-xs text-gray-500">{inputLabel(batch, t)}</p>
          <dl className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1 text-xs text-gray-600 sm:grid-cols-4">
            <div>
              <dt className="text-gray-400">{t('batches.row.requests', 'Requests')}</dt>
              <dd>
                {fill(t('batches.row.progress', '{done} of {total} done'), {
                  done: String(done),
                  total: String(total),
                })}
                {failed > 0
                  ? ` · ${fill(t('batches.row.failed', '{failed} failed'), { failed: String(failed) })}`
                  : ''}
              </dd>
            </div>
            <div>
              <dt className="text-gray-400">{t('batches.row.tokens', 'Tokens')}</dt>
              <dd>
                {fill(t('batches.row.tokens_value', '{input} in · {output} out'), {
                  input: batch.usage.input_tokens.toLocaleString(),
                  output: batch.usage.output_tokens.toLocaleString(),
                })}
              </dd>
            </div>
            <div>
              <dt className="text-gray-400">{t('batches.row.cost', 'Known cost')}</dt>
              <dd>
                ${batch.cost_usd.toFixed(4)}
                {batch.cost_unknown_attempts > 0 && (
                  <span
                    className="ml-1 text-amber-700"
                    title={t(
                      'batches.row.unknown_cost_hint',
                      'Attempts whose result was lost after the request was sent; their cost is unknown and not included.',
                    )}
                  >
                    {fill(t('batches.row.unknown_cost', '+ {count} unknown'), {
                      count: String(batch.cost_unknown_attempts),
                    })}
                  </span>
                )}
              </dd>
            </div>
            <div>
              <dt className="text-gray-400">{t('batches.row.created', 'Created')}</dt>
              <dd>{formatTime(batch.created_at)}</dd>
            </div>
          </dl>
          {batch.validation_error?.data?.length ? (
            <p className="mt-2 text-xs text-red-700">
              {batch.validation_error.data
                .slice(0, 3)
                .map((problem) =>
                  problem.line
                    ? fill(t('batches.row.line_problem', 'Line {line}: {message}'), {
                        line: String(problem.line),
                        message: problem.message ?? '',
                      })
                    : (problem.message ?? ''),
                )
                .join(' · ')}
            </p>
          ) : null}
          {batch.error?.message ? (
            <p className="mt-2 text-xs text-red-700">{batch.error.message}</p>
          ) : null}
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          {batch.results_available && batch.input !== 'file' && (
            <Button
              size="sm"
              variant="secondary"
              disabled={busy}
              onClick={() => onDownload(batch, 'all')}
            >
              {t('batches.action.download', 'Download results')}
            </Button>
          )}
          {batch.results_available && batch.output_file_id && (
            <Button
              size="sm"
              variant="secondary"
              disabled={busy}
              onClick={() => onDownload(batch, 'output')}
            >
              {t('batches.action.download_output', 'Download outputs')}
            </Button>
          )}
          {batch.results_available && batch.error_file_id && (
            <Button
              size="sm"
              variant="secondary"
              disabled={busy}
              onClick={() => onDownload(batch, 'error')}
            >
              {t('batches.action.download_errors', 'Download errors')}
            </Button>
          )}
          {isCancellable(batch) && (
            <Button size="sm" variant="secondary" disabled={busy} onClick={() => onCancel(batch)}>
              {t('batches.action.cancel', 'Cancel')}
            </Button>
          )}
          <Button size="sm" variant="danger" disabled={busy} onClick={() => onDelete(batch)}>
            {t('batches.action.delete', 'Delete')}
          </Button>
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
 * their webhook settings. Batches are created through the API, not here.
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
  const { batches, hasMore, loading, loadingMore, error, reload, loadMore, replace, remove } =
    usePagedBatches((after) => listMyBatches(after), 'mine');
  // Only a known "no": without an answer the gateway decides each action.
  const cannotSubmit = capabilities?.role_may_submit === false;
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
      <PageHeader
        subtitle={t(
          'batches.subtitle',
          'Batches you submitted through /v1/batches, as direct requests or a JSONL file. Results are kept for a limited time after a batch ends.',
        )}
      >
        <Button variant="secondary" onClick={() => void reload()} disabled={loading}>
          {t('batches.refresh', 'Refresh')}
        </Button>
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
        {error && (
          <p role="alert" className="mb-3 text-sm text-red-700">
            {error}
          </p>
        )}
        {loading ? (
          <p className="text-sm text-gray-500">{t('batches.loading', 'Loading batches…')}</p>
        ) : batches.length === 0 && !error ? (
          <p className="text-sm text-gray-500">
            {cannotSubmit
              ? t('batches.empty_existing', 'You have no batches.')
              : t(
                  'batches.empty',
                  'You have no batches. Submit one through the API to see it here.',
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
