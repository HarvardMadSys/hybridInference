'use client';

import { useEffect, useMemo, useRef } from 'react';
import type { GeoAnalyticsResponse } from '@/lib/api/admin';
import { buildColumnIndex, type GeoMetricModel } from './geoMath';

function windowLabel(data: GeoAnalyticsResponse): string {
  const first = new Date(data.hours_index[0] ?? '');
  const last = new Date(data.hours_index[data.hours_index.length - 1] ?? '');
  if (Number.isNaN(first.getTime()) || Number.isNaN(last.getTime())) return 'unknown window';
  const format = (date: Date) =>
    date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
  const days = Math.max(1, Math.round(data.hours_index.length / 24));
  return `${format(first)} – ${format(last)} (${days} ${days === 1 ? 'day' : 'days'}, hourly)`;
}

function metricUnit(metric: GeoMetricModel['metric']): string {
  return metric === 'tout' ? 'output tokens' : 'requests';
}

/** All method caveats, totals, and attribution live here — never on the page chrome. */
export function DataDetailsDialog({
  open,
  onClose,
  data,
  metricModel,
}: {
  open: boolean;
  onClose: () => void;
  data: GeoAnalyticsResponse;
  metricModel: GeoMetricModel;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    closeRef.current?.focus();
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, open]);

  const totals = useMemo(() => {
    const bucketIndex = buildColumnIndex(data.bucket_cols);
    let located = 0;
    let outputTokens = 0;
    for (const hour of data.hours) {
      for (const row of hour.b) {
        const country = String(row[bucketIndex.c] ?? '');
        const requests = Number(row[bucketIndex.n] ?? 0);
        outputTokens += Number(row[bucketIndex.tout] ?? 0);
        if (country && !country.startsWith('?')) located += requests;
      }
    }
    return { located, outputTokens };
  }, [data]);

  if (!open) return null;

  const locatedShare =
    data.meta.rows_total > 0 ? Math.round((100 * totals.located) / data.meta.rows_total) : 0;
  const generated = new Date(data.meta.generated_at);
  const generatedLabel = Number.isNaN(generated.getTime())
    ? 'unknown'
    : `${generated.toISOString().slice(0, 16).replace('T', ' ')} UTC`;
  const cap = Math.round(metricModel.countryHourP99).toLocaleString('en-US');

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-gray-950/40 p-4"
      onClick={onClose}
      role="presentation"
    >
      <div
        aria-label="Data details"
        aria-modal="true"
        className="w-full max-w-md rounded-xl border border-gray-200 bg-white p-5 shadow-xl"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
      >
        <h3 className="text-sm font-semibold text-gray-900">Data details</h3>
        <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-[13px]">
          <dt className="text-gray-500">Window</dt>
          <dd className="text-gray-800">{windowLabel(data)}</dd>
          <dt className="text-gray-500">Requests</dt>
          <dd className="text-gray-800">{data.meta.rows_total.toLocaleString('en-US')}</dd>
          <dt className="text-gray-500">Output tokens</dt>
          <dd className="text-gray-800">{totals.outputTokens.toLocaleString('en-US')}</dd>
          <dt className="text-gray-500">Located</dt>
          <dd className="text-gray-800">{locatedShare}% of requests resolved to a country</dd>
          <dt className="text-gray-500">Updated</dt>
          <dd className="text-gray-800">{generatedLabel}</dd>
          <dt className="text-gray-500">Scale</dt>
          <dd className="text-gray-800">
            fixed across the window · values above the 99th percentile ({cap}{' '}
            {metricUnit(metricModel.metric)}/country·hour) are capped
          </dd>
          <dt className="text-gray-500">Geolocation</dt>
          <dd className="text-gray-800">
            {data.meta.geoip.attribution ? (
              <a
                className="underline decoration-gray-300 underline-offset-2 hover:text-gray-900"
                href={data.meta.geoip.attribution.url}
                rel="noopener noreferrer"
                target="_blank"
              >
                {data.meta.geoip.attribution.label}
              </a>
            ) : data.meta.geoip.country ? (
              'offline GeoIP database'
            ) : (
              'unavailable — origins shown as unknown'
            )}
          </dd>
        </dl>
        <p className="mt-3 border-t border-gray-100 pt-3 text-xs leading-relaxed text-gray-500">
          Origins are network origins estimated from request IP addresses; they may differ from
          user location (VPNs, proxies, cloud runners).
          {data.meta.source === 'synthetic-demo' && (
            <strong className="block pt-1 font-semibold text-amber-700">
              This view is running on synthetic demo data.
            </strong>
          )}
        </p>
        <div className="mt-4 text-right">
          <button
            ref={closeRef}
            type="button"
            className="rounded-md border border-gray-200 bg-white px-3.5 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50"
            onClick={onClose}
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
