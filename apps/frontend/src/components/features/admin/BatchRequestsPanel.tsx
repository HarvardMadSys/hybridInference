'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { AdminLoading, AdminNotice } from '@/components/features/admin/AdminPrimitives';
import { BUTTON_SECONDARY, PILL } from '@/components/features/admin/adminStyles';
import { FoldedText } from '@/components/features/admin/requestContent';
import { getAdminBatchRequests, type AdminBatchItem } from '@/lib/api/batches';
import { getErrorMessage } from '@/lib/utils/errors';

const STATUS_TONE: Record<string, string> = {
  completed: 'bg-green-50 text-green-700',
  failed: 'bg-red-50 text-red-700',
  expired: 'bg-amber-50 text-amber-800',
  cancelled: 'bg-gray-100 text-gray-700',
};

function json(value: unknown): string | null {
  return value === null || value === undefined ? null : JSON.stringify(value);
}

/**
 * One batch's requests with their prompts and answers, a page at a time, for
 * administrators. The gateway refuses where it keeps no prompts, and records
 * the first page as an audited read.
 */
export function BatchRequestsPanel({ batchId }: { batchId: string }) {
  const [items, setItems] = useState<AdminBatchItem[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);

  const load = useCallback(
    async (after: number) => {
      const mine = ++generation.current;
      setLoading(true);
      setError(null);
      try {
        const page = await getAdminBatchRequests(batchId, after);
        if (mine !== generation.current) return;
        setItems((shown) => (after === 0 ? page.data : [...shown, ...page.data]));
        setNext(page.has_more ? page.last_id : null);
      } catch (err) {
        if (mine === generation.current) setError(getErrorMessage(err));
      } finally {
        if (mine === generation.current) setLoading(false);
      }
    },
    [batchId],
  );

  useEffect(() => {
    void load(0);
  }, [load]);

  return (
    <div className="space-y-3" data-testid={`requests-${batchId}`}>
      <p className="text-[11px] text-gray-500">
        Opening a batch&apos;s requests is recorded in the audit log.
      </p>
      {error && <AdminNotice tone="error">{error}</AdminNotice>}
      {items.length === 0 && loading ? (
        <AdminLoading label="Loading requests" />
      ) : items.length === 0 && !error ? (
        <p className="text-[12px] text-gray-500">This batch has no stored requests.</p>
      ) : (
        <ol className="space-y-3">
          {items.map((item) => (
            <li
              key={item.id}
              className="rounded-md border border-gray-200 bg-white p-3 text-[12px]"
              data-testid={`request-${item.custom_id}`}
            >
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <span className="font-mono text-gray-900">{item.custom_id}</span>
                <span
                  className={`${PILL} ${STATUS_TONE[item.status] ?? 'bg-blue-50 text-blue-700'}`}
                >
                  {item.status}
                  {item.status_code ? ` · ${item.status_code}` : ''}
                </span>
                <span className="text-gray-500">{item.model_id}</span>
                {item.attempts > 1 && (
                  <span className="text-gray-500">{item.attempts} attempts</span>
                )}
                {item.prompt_tokens !== null && (
                  <span className="text-gray-500">
                    {item.prompt_tokens.toLocaleString('en-US')} in ·{' '}
                    {(item.completion_tokens ?? 0).toLocaleString('en-US')} out
                  </span>
                )}
              </div>
              <div className="grid gap-1">
                <FoldedText label="Prompt" value={json(item.request)} />
                {item.response !== null && item.response !== undefined && (
                  <FoldedText label="Response" value={json(item.response)} />
                )}
                {item.error && (
                  <div className="text-red-700">
                    <span className="text-gray-500">Error:</span>{' '}
                    {item.error.code ? `${item.error.code}: ` : ''}
                    {item.error.message ?? ''}
                  </div>
                )}
              </div>
            </li>
          ))}
        </ol>
      )}
      {next !== null && (
        <button
          type="button"
          className={BUTTON_SECONDARY}
          disabled={loading}
          aria-busy={loading || undefined}
          onClick={() => void load(next)}
        >
          Show more requests
        </button>
      )}
    </div>
  );
}
