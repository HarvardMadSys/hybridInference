import type { Translate } from '@/components/providers/useT';
import type { BatchCategory, BatchStatus, ConsoleBatch, ConsoleTarget } from '@/lib/api/batches';
import { fill } from '@/lib/utils/interpolate';

/** What a batch runs, as the console names it: the task type, whatever API shape it uses. */
export function targetLabel(
  batch: Pick<ConsoleBatch, 'target' | 'endpoint'>,
  t: Translate,
): string {
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

/** A console type in words, with what it does. */
export function consoleTypeLabel(target: ConsoleTarget, t: Translate): string {
  return target === 'chat'
    ? t('batches.create.type.chat', 'Text generation (Chat completions)')
    : t('batches.create.type.embeddings', 'Text embeddings (Embeddings)');
}

/** How the batch's requests arrived: an input method of the one batch API, not an API. */
export function inputLabel(batch: Pick<ConsoleBatch, 'input'>, t: Translate): string {
  return batch.input === 'file'
    ? t('batches.input.file', 'JSONL file')
    : t('batches.input.inline', 'Direct requests');
}

/** Where the batch was created. */
export function originLabel(batch: Pick<ConsoleBatch, 'created_via'>, t: Translate): string {
  return batch.created_via === 'console'
    ? t('batches.origin.console', 'Created in the console')
    : t('batches.origin.api', 'Created through the API');
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

export const ALL_STATUSES: BatchStatus[] = [
  'validating',
  'in_progress',
  'finalizing',
  'completed',
  'failed',
  'expired',
  'cancelling',
  'cancelled',
];

/** Done / total: completed, failed and cancelled requests are done. */
export function progress(batch: ConsoleBatch): { done: number; total: number; failed: number } {
  const counts = batch.request_counts;
  return {
    done: counts.completed + counts.failed + batch.cancelled,
    total: counts.total,
    failed: counts.failed,
  };
}

/**
 * A batch's requests by outcome, the same for every batch: `failed` counts
 * expired ones too, so they are taken out of it here and shown on their own.
 */
export function outcomeCounts(batch: ConsoleBatch): {
  succeeded: number;
  failed: number;
  expired: number;
  cancelled: number;
  unfinished: number;
  total: number;
} {
  const { total, completed, failed } = batch.request_counts;
  return {
    succeeded: completed,
    failed: Math.max(failed - batch.expired, 0),
    expired: batch.expired,
    cancelled: batch.cancelled,
    unfinished: Math.max(total - completed - failed - batch.cancelled, 0),
    total,
  };
}

/** Why a batch is paused, or null when it is not. */
export function pauseText(batch: ConsoleBatch, t: Translate): string | null {
  if (!batch.paused_until) return null;
  const until = new Date(batch.paused_until * 1000).toLocaleString();
  if (batch.paused_reason === 'quota') {
    return fill(
      t(
        'batches.paused.quota',
        'Paused: your daily quota is spent. It resumes after {until} (when the quota renews at 00:00 UTC) or once your quota is raised.',
      ),
      { until },
    );
  }
  if (batch.paused_reason === 'role') {
    return fill(
      t(
        'batches.paused.role',
        'Paused: your plan no longer includes Batch. It is checked again after {until}; you can still cancel it or download what finished.',
      ),
      { until },
    );
  }
  return fill(t('batches.paused.other', 'Paused until {until}.'), { until });
}

export function formatTime(epoch: number | null | undefined): string {
  if (!epoch) return '—';
  return new Date(epoch * 1000).toLocaleString();
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

/** A batch's category in words: the badge the list and the details show. */
export function categoryLabel(category: BatchCategory, t: Translate): string {
  switch (category) {
    case 'processing':
      return t('batches.category.processing', 'Processing');
    case 'completed':
      return t('batches.category.completed', 'Completed');
    case 'issues':
      return t('batches.category.issues', 'With issues');
    case 'cancelled':
      return t('batches.category.cancelled', 'Cancelled');
    default:
      return category;
  }
}

/** The badge colour of each category; "completed" is green only when every request succeeded. */
export const CATEGORY_TONE: Record<BatchCategory, string> = {
  processing: 'bg-blue-50 text-blue-700 ring-blue-200',
  completed: 'bg-green-50 text-green-700 ring-green-200',
  issues: 'bg-amber-50 text-amber-800 ring-amber-200',
  cancelled: 'bg-gray-100 text-gray-700 ring-gray-200',
};

/**
 * What the category leaves out, in a few words: the stage of a batch in
 * progress, or why one has issues. Null when the category says it all.
 */
export function categoryNote(batch: ConsoleBatch, t: Translate): string | null {
  switch (batch.status) {
    case 'validating':
      return t('batches.note.validating', 'Checking the data');
    case 'finalizing':
      return t('batches.note.finalizing', 'Preparing results');
    case 'cancelling':
      return t('batches.note.cancelling', 'Cancelling: requests already running will finish');
    case 'failed':
      return t('batches.note.failed', 'The batch failed');
    case 'expired':
      return t('batches.note.expired', 'Execution window ran out');
    case 'completed':
      if (batch.category !== 'issues') return null;
      return batch.request_counts.completed === 0
        ? t('batches.note.none_succeeded', 'No request succeeded')
        : t('batches.note.partial', 'Some requests failed');
    default:
      return null;
  }
}
