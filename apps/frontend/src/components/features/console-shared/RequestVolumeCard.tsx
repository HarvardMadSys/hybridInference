'use client';

import type { ReactNode } from 'react';
import { useT } from '@/components/providers/useT';
import { formatLatency } from './format';

/**
 * The request volume of one lookback window: how many requests, how many of
 * them ended well and how many failed, and the average latency.
 *
 * The caller reads the figures and names the window. A page that counts more
 * kinds of requests adds its own lines to the right-hand column, and a page
 * that has the window in parts draws them under the figures as `children`.
 */
export function RequestVolumeCard({
  label,
  total,
  ok,
  failed,
  averageLatencyMs,
  afterFailed,
  afterAverage,
  formatNumber = (value) => value.toLocaleString(),
  children,
}: {
  /** The window, already in words: "Last hour". */
  label: string;
  total: number;
  ok: number;
  failed: number;
  averageLatencyMs?: number | null;
  /** More lines of the right-hand column, after the failed count. */
  afterFailed?: ReactNode;
  /** More lines of the right-hand column, after the latency. */
  afterAverage?: ReactNode;
  formatNumber?: (value: number) => string;
  children?: ReactNode;
}) {
  const t = useT();
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-[12px] font-medium text-gray-500">{label}</div>
          <div className="mt-1 text-[24px] font-bold tabular-nums text-gray-900">
            {formatNumber(total)}
          </div>
          <div className="text-[11px] text-gray-400">
            {t('console.volume.requests', 'requests')}
          </div>
        </div>
        <div className="text-right text-[11px] text-gray-400">
          <div>
            <span className="text-emerald-600">{formatNumber(ok)}</span>{' '}
            {t('console.volume.ok', 'ok')}
          </div>
          <div>
            <span className="text-red-500">{formatNumber(failed)}</span>{' '}
            {t('console.volume.failed', 'err')}
          </div>
          {afterFailed}
          <div>
            {formatLatency(averageLatencyMs)} {t('console.volume.average', 'avg')}
          </div>
          {afterAverage}
        </div>
      </div>
      {children}
    </div>
  );
}
