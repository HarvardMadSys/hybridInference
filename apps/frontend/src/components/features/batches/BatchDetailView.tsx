'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';

import { Button } from '@/components/ui/Button';
import { useAuth } from '@/components/providers';
import { useT, type Translate } from '@/components/providers/useT';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import {
  cancelMyBatch,
  deleteMyBatch,
  downloadMyResults,
  downloadSourceFile,
  downloadStoredRequests,
  downloadUnfinishedRequests,
  FINAL_STATUSES,
  getMyBatch,
  isCancellable,
  type BatchDetail,
  type ResultKind,
} from '@/lib/api/batches';
import { useBatchCapabilities } from '@/lib/hooks/queries/useBatchCapabilities';
import { getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';

import {
  formatBytes,
  formatTime,
  inputLabel,
  originLabel,
  outcomeCounts,
  pauseText,
  statusLabel,
  targetLabel,
} from './batchText';

function copyBlockedText(detail: BatchDetail, t: Translate): string | null {
  if (detail.copy.reason === 'inline_input') {
    return t(
      'batches.detail.copy_inline',
      'This batch was submitted as direct requests, so there is no input file to reuse. Copying it is not available yet.',
    );
  }
  if (detail.copy.reason === 'unsupported_target') {
    return t(
      'batches.detail.copy_target',
      'Batches of this type cannot be created in the console yet; submit them through /v1/batches. You can still manage this one and download its results.',
    );
  }
  return null;
}

function sourceStateText(state: string, t: Translate): string {
  switch (state) {
    case 'available':
      return t('batches.detail.source.available', 'Kept');
    case 'deleted':
      return t('batches.detail.source.deleted', 'Deleted');
    case 'expired':
      return t('batches.detail.source.expired', 'Expired');
    default:
      return t('batches.detail.source.missing', 'No longer stored');
  }
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-1 gap-1 py-2 sm:grid-cols-3 sm:gap-4">
      <dt className="text-sm text-gray-500">{label}</dt>
      <dd className="break-words text-sm text-gray-900 sm:col-span-2">{children}</dd>
    </div>
  );
}

/** One of the signed-in user's batches: its counts, configuration, downloads and actions. */
export function BatchDetailView({ batchId }: { batchId: string }) {
  const t = useT();
  const router = useRouter();
  const toast = useAccountToast();
  const { state } = useAuth();
  const userId = state.user?.id ?? null;
  const { capabilities } = useBatchCapabilities();
  const [detail, setDetail] = useState<BatchDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const forUser = userId;
    try {
      const found = await getMyBatch(batchId);
      // An answer for another account (a switch in another tab) is never shown here.
      if (forUser !== userId || (found.user_id && found.user_id !== forUser)) return;
      setDetail(found);
      setError(null);
    } catch (err) {
      setError(getErrorMessage(err));
    }
  }, [batchId, userId]);

  useEffect(() => {
    setDetail(null);
    if (userId) void load();
  }, [load, userId]);

  useEffect(() => {
    // Follow an unfinished batch while the page is open.
    if (!detail || FINAL_STATUSES.has(detail.status)) return;
    const timer = setInterval(() => void load(), 5000);
    return () => clearInterval(timer);
  }, [detail, load]);

  async function act(run: () => Promise<void>) {
    setBusy(true);
    try {
      await run();
    } catch (err) {
      toast.error(getErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  if (error && !detail) {
    return (
      <div className="space-y-4">
        <Link href="/dashboard/batches" className="text-sm text-gray-500 hover:text-gray-900">
          ← {t('batches.detail.back', 'Batches')}
        </Link>
        <p role="alert" className="text-sm text-red-700">
          {error}
        </p>
      </div>
    );
  }
  if (!detail) {
    return <p className="text-sm text-gray-500">{t('batches.loading', 'Loading batches…')}</p>;
  }

  const counts = outcomeCounts(detail);
  const paused = pauseText(detail, t);
  const mayCreate = capabilities?.role_may_submit !== false;
  const copyBlocked = copyBlockedText(detail, t);
  const exportable =
    detail.results_available &&
    (detail.target === 'chat' || detail.target === 'embeddings') &&
    counts.failed + counts.expired + counts.cancelled > 0;
  const creation = detail.creation;

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <Link href="/dashboard/batches" className="text-sm text-gray-500 hover:text-gray-900">
            ← {t('batches.detail.back', 'Batches')}
          </Link>
          <h1 className="mt-1 break-words text-2xl font-semibold tracking-tight text-gray-900">
            {detail.name || detail.id}
          </h1>
          <p className="mt-1 text-sm text-gray-600">
            <span className="font-medium">{targetLabel(detail, t)}</span>
            {detail.models.length ? ` · ${detail.models.join(', ')}` : ''} ·{' '}
            {statusLabel(detail.status, t)}
          </p>
          {detail.description && (
            <p className="mt-1 whitespace-pre-wrap text-sm text-gray-500">{detail.description}</p>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          {isCancellable(detail) && (
            <Button
              variant="secondary"
              disabled={busy}
              onClick={() => {
                if (
                  !window.confirm(
                    t(
                      'batches.confirm.cancel',
                      'Cancel this batch? Requests that have not started will not run; running ones finish.',
                    ),
                  )
                )
                  return;
                void act(async () => {
                  await cancelMyBatch(detail.id);
                  await load();
                });
              }}
            >
              {t('batches.action.cancel', 'Cancel')}
            </Button>
          )}
          {mayCreate && detail.copy.state !== 'unavailable' && (
            <Link
              href={`/dashboard/batches/new?copy=${encodeURIComponent(detail.id)}`}
              className="inline-flex items-center rounded-lg px-4 py-2 text-sm font-medium text-blue-700 ring-1 ring-inset ring-blue-200 hover:bg-blue-50"
            >
              {t('batches.action.copy', 'Copy as new batch')}
            </Link>
          )}
          <Button
            variant="danger"
            disabled={busy}
            onClick={() => {
              if (
                !window.confirm(
                  t(
                    'batches.confirm.delete',
                    'Delete this batch and its results? This cannot be undone.',
                  ),
                )
              )
                return;
              void act(async () => {
                await deleteMyBatch(detail.id);
                toast.success(t('batches.toast.deleted', 'Batch deleted.'));
                router.push('/dashboard/batches');
              });
            }}
          >
            {t('batches.action.delete', 'Delete')}
          </Button>
        </div>
      </div>

      {paused && (
        <div
          role="note"
          className="rounded-lg bg-amber-50 p-4 text-sm text-amber-900 ring-1 ring-amber-200"
        >
          ⚠ {paused}
        </div>
      )}
      {mayCreate && copyBlocked && (
        <p role="note" className="text-sm text-gray-500">
          {copyBlocked}
        </p>
      )}
      {detail.copy.state === 'reupload' && mayCreate && (
        <p role="note" className="text-sm text-gray-500">
          {t(
            'batches.detail.copy_reupload',
            'Its source file is no longer stored. Copying keeps the settings and asks you to upload the file again.',
          )}
        </p>
      )}

      <section className="rounded-xl bg-white p-6 shadow-sm ring-1 ring-gray-200">
        <h2 className="text-base font-semibold text-gray-900">
          {t('batches.detail.requests', 'Requests')}
        </h2>
        <dl className="mt-3 grid grid-cols-2 gap-3 text-sm sm:grid-cols-6">
          {(
            [
              ['succeeded', t('batches.detail.count.succeeded', 'Succeeded')],
              ['failed', t('batches.detail.count.failed', 'Failed')],
              ['expired', t('batches.detail.count.expired', 'Expired unrun')],
              ['cancelled', t('batches.detail.count.cancelled', 'Cancelled')],
              ['unfinished', t('batches.detail.count.unfinished', 'Not finished')],
              ['total', t('batches.detail.count.total', 'Total')],
            ] as const
          ).map(([key, label]) => (
            <div key={key} className="rounded-lg bg-gray-50 p-3">
              <dt className="text-xs text-gray-500">{label}</dt>
              <dd className="mt-1 text-lg font-semibold text-gray-900" data-testid={`count-${key}`}>
                {counts[key].toLocaleString()}
              </dd>
            </div>
          ))}
        </dl>
        <dl className="mt-4 divide-y divide-gray-100">
          <Row label={t('batches.detail.cost', 'Known cost')}>
            ${detail.cost_usd.toFixed(4)}
            {detail.cost_unknown_attempts > 0 &&
              ` · ${fill(
                t(
                  'batches.detail.unknown_cost',
                  '{count} attempt(s) of unknown cost, not included (the request was sent but its result was lost)',
                ),
                { count: String(detail.cost_unknown_attempts) },
              )}`}
          </Row>
          <Row label={t('batches.row.tokens', 'Tokens')}>
            {fill(t('batches.row.tokens_value', '{input} in · {output} out'), {
              input: detail.usage.input_tokens.toLocaleString(),
              output: detail.usage.output_tokens.toLocaleString(),
            })}
          </Row>
          <Row label={t('batches.detail.id', 'Batch ID')}>
            <code>{detail.id}</code>
          </Row>
          <Row label={t('batches.detail.how', 'How it was submitted')}>
            {inputLabel(detail, t)} · {originLabel(detail, t)}
          </Row>
          <Row label={t('batches.row.created', 'Created')}>{formatTime(detail.created_at)}</Row>
          <Row label={t('batches.detail.window', 'Start requests by')}>
            {formatTime(detail.expires_at)}
          </Row>
          <Row label={t('batches.detail.ended', 'Ended')}>{formatTime(detail.ended_at)}</Row>
          <Row label={t('batches.row.results_until', 'Results kept until')}>
            {formatTime(detail.results_expires_at)}
          </Row>
          {detail.error?.message && (
            <Row label={t('batches.detail.error', 'Error')}>{detail.error.message}</Row>
          )}
          {detail.validation_error?.data?.length ? (
            <Row label={t('batches.detail.validation', 'Input problems')}>
              {detail.validation_error.data
                .slice(0, 5)
                .map((problem) =>
                  problem.line
                    ? fill(t('batches.row.line_problem', 'Line {line}: {message}'), {
                        line: String(problem.line),
                        message: problem.message ?? '',
                      })
                    : (problem.message ?? ''),
                )
                .join(' · ')}
            </Row>
          ) : null}
        </dl>
        <p className="mt-3 text-xs text-gray-500">
          {t(
            'batches.detail.cancel_semantics',
            'Cancelling stops requests that have not started; requests already running finish and are billed. Results so far stay available to download.',
          )}
        </p>
      </section>

      <section className="rounded-xl bg-white p-6 shadow-sm ring-1 ring-gray-200">
        <h2 className="text-base font-semibold text-gray-900">
          {t('batches.detail.config', 'Configuration')}
        </h2>
        <dl className="mt-2 divide-y divide-gray-100">
          {detail.source && (
            <Row label={t('batches.detail.source', 'Source file')}>
              {detail.source.filename ?? detail.source.id} ·{' '}
              {sourceStateText(detail.source.state, t)}
              {detail.source.state === 'available' && detail.source.expires_at
                ? ` · ${fill(t('batches.detail.source_until', 'kept until {date}'), {
                    date: formatTime(detail.source.expires_at),
                  })}`
                : ''}
              {detail.source.sha256 && (
                <span className="block text-xs text-gray-500">
                  SHA-256 <code>{detail.source.sha256.slice(0, 16)}…</code>
                  {detail.source.bytes ? ` · ${formatBytes(detail.source.bytes)}` : ''}
                </span>
              )}
            </Row>
          )}
          {creation && (
            <>
              <Row label={t('batches.detail.model', 'Model')}>
                {creation.confirmed_model
                  ? creation.confirmed_model.requested === creation.confirmed_model.canonical
                    ? creation.confirmed_model.canonical
                    : `${creation.confirmed_model.requested} → ${creation.confirmed_model.canonical}`
                  : '—'}
                <span className="block text-xs text-gray-500">
                  {creation.overrides.model
                    ? t('batches.detail.model_override', 'Set for every request in the console')
                    : t('batches.detail.model_file', "The file's model")}
                </span>
              </Row>
              <Row label={t('batches.detail.overrides', 'Parameters set for every request')}>
                {Object.entries(creation.overrides)
                  .filter(([key]) => key !== 'model')
                  .map(([key, value]) => `${key} = ${String(value)}`)
                  .join(' · ') || t('batches.detail.no_overrides', "None: the file's values")}
              </Row>
              <Row label={t('batches.detail.credential', 'Runs under API key')}>
                {creation.credential_key_prefix ? `${creation.credential_key_prefix}…` : '—'}
              </Row>
              {creation.copied_from && (
                <Row label={t('batches.detail.copied_from', 'Copied from')}>
                  <Link
                    className="text-blue-700 hover:underline"
                    href={`/dashboard/batches/${encodeURIComponent(creation.copied_from)}`}
                  >
                    {creation.copied_from}
                  </Link>
                </Row>
              )}
            </>
          )}
        </dl>
      </section>

      <section className="rounded-xl bg-white p-6 shadow-sm ring-1 ring-gray-200">
        <h2 className="text-base font-semibold text-gray-900">
          {t('batches.detail.downloads', 'Downloads')}
        </h2>
        {!detail.results_available && (
          <p className="mt-1 text-sm text-gray-500">
            {t(
              'batches.detail.results_pending',
              'Results can be downloaded once the batch has finished.',
            )}
          </p>
        )}
        <div className="mt-3 flex flex-wrap gap-2">
          {detail.results_available && detail.input !== 'file' && (
            <DownloadButton
              busy={busy}
              label={t('batches.action.download', 'Download results')}
              run={() => act(() => downloadMyResults(detail.id, 'all' as ResultKind))}
            />
          )}
          {detail.results_available && detail.input === 'file' && detail.output_file_id && (
            <DownloadButton
              busy={busy}
              label={t('batches.action.download_output', 'Download outputs')}
              run={() => act(() => downloadMyResults(detail.id, 'output'))}
            />
          )}
          {detail.results_available && detail.input === 'file' && detail.error_file_id && (
            <DownloadButton
              busy={busy}
              label={t('batches.action.download_errors', 'Download errors')}
              run={() => act(() => downloadMyResults(detail.id, 'error'))}
            />
          )}
          {detail.phase !== 'validating' && detail.request_counts.total > 0 && (
            <DownloadButton
              busy={busy}
              label={t('batches.action.download_requests', 'Download stored requests')}
              run={() => act(() => downloadStoredRequests(detail.id))}
            />
          )}
          {exportable && (
            <DownloadButton
              busy={busy}
              label={t('batches.action.download_unfinished', 'Export unfinished requests')}
              run={() => act(() => downloadUnfinishedRequests(detail.id))}
            />
          )}
          {detail.source?.state === 'available' && detail.source.id && (
            <DownloadButton
              busy={busy}
              label={t('batches.action.download_source', 'Download source file')}
              run={() =>
                act(() =>
                  downloadSourceFile(
                    detail.source!.id!,
                    detail.source!.filename ?? `${detail.source!.id}.jsonl`,
                  ),
                )
              }
            />
          )}
        </div>
        {exportable && (
          <p className="mt-2 text-xs text-gray-500">
            {t(
              'batches.detail.unfinished_hint',
              'Unfinished requests are the failed, expired and cancelled ones, as they were stored: fix them and upload the file as a new batch. Nothing runs until you submit it. The error file explains failures and cannot be submitted again as is.',
            )}
          </p>
        )}
      </section>
    </div>
  );
}

function DownloadButton({
  busy,
  label,
  run,
}: {
  busy: boolean;
  label: string;
  run: () => void | Promise<void>;
}) {
  return (
    <Button size="sm" variant="secondary" disabled={busy} onClick={() => void run()}>
      {label}
    </Button>
  );
}
