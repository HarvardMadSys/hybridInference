'use client';

import { useCallback, useEffect, useState } from 'react';
import toast from 'react-hot-toast';
import {
  AdminMetricDistribution,
  AdminPerformanceMetricsWindow,
  getPerformanceMetrics,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { ProviderPerformanceTab } from '@/app/dashboard/admin/ProviderPerformanceTab';

function formatLatency(ms?: number | null): string {
  if (ms == null) return '—';
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms)}ms`;
}

function formatTokens(n: number): string {
  return Math.round(n).toLocaleString();
}

function formatThroughput(n: number): string {
  if (n >= 1000) {
    const k = n / 1000;
    return `${k.toFixed(n % 1000 === 0 ? 0 : 1)}k tok/s`;
  }
  if (n >= 100) return `${Math.round(n)} tok/s`;
  return `${n.toFixed(1)} tok/s`;
}

function formatBucketEdge(value: number, kind: 'tokens' | 'ms' | 'tps'): string {
  const formatK = (v: number): string => `${(v / 1000).toFixed(v % 1000 === 0 ? 0 : 1)}k`;
  if (kind === 'tokens') {
    return value >= 1000 ? formatK(value) : value.toLocaleString();
  }
  if (kind === 'tps') {
    return formatThroughput(value);
  }
  return value >= 1000 ? `${formatK(value)}s` : `${value}ms`;
}

function PerformanceMetricsCard({ metric }: { metric: AdminPerformanceMetricsWindow }) {
  const rows: Array<{
    title: string;
    dist: AdminMetricDistribution;
    kind: 'tokens' | 'ms' | 'tps';
  }> = [
    { title: 'Prompt tokens', dist: metric.prompt_tokens, kind: 'tokens' },
    { title: 'Response tokens', dist: metric.completion_tokens, kind: 'tokens' },
    { title: 'TTFT', dist: metric.ttft_ms, kind: 'ms' },
    { title: 'Throughput', dist: metric.throughput_tps, kind: 'tps' },
  ];
  const formatValue = (v: number | null | undefined, kind: 'tokens' | 'ms' | 'tps'): string => {
    if (v == null) return '—';
    return kind === 'ms'
      ? formatLatency(v)
      : kind === 'tps'
        ? formatThroughput(v)
        : formatTokens(v);
  };
  return (
    <div className="rounded-xl border border-gray-200 bg-white px-3 py-2.5 shadow-sm">
      <div className="flex items-center justify-between">
        <div className="text-[12px] font-semibold text-gray-900">{metric.label}</div>
        <div className="text-[10px] text-gray-400">{metric.window_minutes}m window</div>
      </div>
      <table className="mt-2 w-full">
        <thead>
          <tr className="text-[10px] uppercase tracking-wider text-gray-400 font-medium">
            <th className="py-1 text-left">Metric</th>
            <th className="py-1 text-right">n</th>
            <th className="py-1 text-right">p50</th>
            <th className="py-1 text-right">p95</th>
            <th className="py-1 text-right">p99</th>
            <th className="py-1 text-right">Dist</th>
          </tr>
        </thead>
        <tbody className="[&>tr+tr>td]:border-t [&>tr+tr>td]:border-gray-100">
          {rows.map((row) => {
            const maxBucket = Math.max(...row.dist.histogram.map((b) => b.count), 1);
            return (
              <tr key={row.title}>
                <td className="py-1.5 text-[11px] text-gray-600">{row.title}</td>
                <td className="py-1.5 text-right text-[11px] tabular-nums text-gray-900 font-medium">
                  {row.dist.count.toLocaleString()}
                </td>
                <td className="py-1.5 text-right text-[11px] tabular-nums text-gray-900 font-medium">
                  {formatValue(row.dist.p50, row.kind)}
                </td>
                <td className="py-1.5 text-right text-[11px] tabular-nums text-gray-900 font-medium">
                  {formatValue(row.dist.p95, row.kind)}
                </td>
                <td className="py-1.5 text-right text-[11px] tabular-nums text-gray-900 font-medium">
                  {formatValue(row.dist.p99, row.kind)}
                </td>
                <td className="py-1.5 text-right">
                  {(() => {
                    const peakIdx = row.dist.histogram.reduce(
                      (best, b, i, arr) => (b.count > arr[best].count ? i : best),
                      0,
                    );
                    const peak = row.dist.histogram[peakIdx];
                    const peakLower = peak ? formatBucketEdge(peak.lower_bound, row.kind) : '';
                    const peakUpper =
                      peak == null
                        ? ''
                        : peak.upper_bound == null
                          ? '∞'
                          : formatBucketEdge(peak.upper_bound, row.kind);
                    const srSummary =
                      peak && peak.count > 0
                        ? `Distribution peak [${peakLower}, ${peakUpper}) with ${peak.count.toLocaleString()} samples across ${row.dist.histogram.length} buckets`
                        : 'Distribution: no samples';
                    return (
                      <>
                        <span className="sr-only">{srSummary}</span>
                        <div aria-hidden="true" className="ml-auto flex h-5 w-20 items-end gap-px">
                          {row.dist.histogram.map((b, idx) => {
                            const height = b.count === 0 ? 2 : (b.count / maxBucket) * 100;
                            const upperLabel =
                              b.upper_bound == null
                                ? '∞'
                                : formatBucketEdge(b.upper_bound, row.kind);
                            const lowerLabel = formatBucketEdge(b.lower_bound, row.kind);
                            return (
                              <div
                                key={`${idx}-${b.lower_bound}`}
                                className={`min-w-0 flex-1 rounded-t-[1px] ${
                                  b.count === 0 ? 'bg-gray-200' : 'bg-gray-700'
                                }`}
                                style={{ height: `${height}%` }}
                                title={`[${lowerLabel}, ${upperLabel}): ${b.count.toLocaleString()}`}
                              />
                            );
                          })}
                        </div>
                      </>
                    );
                  })()}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function PerformanceTab() {
  const [perfMetrics, setPerfMetrics] = useState<AdminPerformanceMetricsWindow[]>([]);
  const [perfMetricsLoading, setPerfMetricsLoading] = useState(false);

  const loadPerformanceMetrics = useCallback(async () => {
    setPerfMetricsLoading(true);
    try {
      const d = await getPerformanceMetrics();
      setPerfMetrics(d.windows);
    } catch (e) {
      toast.error(getErrorMessage(e));
    } finally {
      setPerfMetricsLoading(false);
    }
  }, []);

  useEffect(() => {
    loadPerformanceMetrics();
  }, [loadPerformanceMetrics]);

  return (
    <div className="mt-5 space-y-6">
      <ProviderPerformanceTab />
      <div>
        <div className="mb-2 flex items-center justify-between">
          <div>
            <h2 className="text-[14px] font-semibold text-gray-900">Performance metrics</h2>
            <p className="text-[11px] text-gray-400">
              Prompt/response length, time-to-first-token, and inter-token latency distributions.
            </p>
          </div>
          {perfMetricsLoading && (
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
          )}
        </div>
        {perfMetrics.length > 0 ? (
          <div className="grid gap-3 lg:grid-cols-2 xl:grid-cols-2">
            {perfMetrics.map((metric) => (
              <PerformanceMetricsCard key={metric.key} metric={metric} />
            ))}
          </div>
        ) : !perfMetricsLoading ? (
          <div className="rounded-xl border border-dashed border-gray-200 py-8 text-center">
            <p className="text-[13px] text-gray-400">No performance metrics available.</p>
          </div>
        ) : null}
      </div>
    </div>
  );
}
