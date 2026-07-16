'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import type { GeoAnalyticsResponse, GeoMetric } from '@/lib/api/admin';
import { getGeoAnalytics } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { GeoStatCards } from './GeoStatCards';
import {
  atlasCountryName,
  GlobeCanvas,
  prepareAtlas,
  type GlobeSelection,
  type GlobeViewRequest,
  type PreparedAtlas,
} from './GlobeCanvas';
import { TrafficRibbon } from './TrafficRibbon';
import {
  buildColumnIndex,
  buildCountryContinentMap,
  CONTINENT_NAMES,
  currentHourStats,
} from './geoMath';

const HOUR_MS = 3_600_000;
const STALE_AFTER_MS = 2 * HOUR_MS;

const metricOptions: { value: GeoMetric; label: string }[] = [
  { value: 'n', label: 'Requests' },
  { value: 'tout', label: 'Output tokens' },
];

const viewPresets: { label: string; rotation: [number, number] }[] = [
  { label: 'AS', rotation: [-100, -28] },
  { label: 'EU', rotation: [-15, -48] },
  { label: 'NA', rotation: [95, -38] },
  { label: 'SA', rotation: [60, 15] },
  { label: 'AF', rotation: [-20, -3] },
  { label: 'OC', rotation: [-145, 25] },
];

function formatCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 10_000) return `${Math.round(value / 1_000)}k`;
  return Math.round(value).toLocaleString('en-US');
}

function formatTimestamp(timestamp: string): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return timestamp || 'unknown time';
  return `${date.toISOString().slice(0, 16).replace('T', ' ')} UTC`;
}

function formatAge(timestamp: string): { label: string; stale: boolean } {
  const generated = new Date(timestamp).getTime();
  if (!Number.isFinite(generated)) return { label: 'generation time unavailable', stale: true };
  const age = Math.max(0, Date.now() - generated);
  if (age < 60_000) return { label: 'generated just now', stale: false };
  if (age < HOUR_MS) return { label: `generated ${Math.floor(age / 60_000)}m ago`, stale: false };
  const hours = Math.floor(age / HOUR_MS);
  return { label: `generated ${hours}h ago`, stale: age > STALE_AFTER_MS };
}

function LoadingState() {
  return (
    <div aria-label="Loading geographic demand" className="mt-6 space-y-3" role="status">
      <div className="h-24 animate-pulse rounded-xl bg-gray-100" />
      <div className="h-[440px] animate-pulse rounded-xl bg-gray-900" />
      <p className="text-center text-sm text-gray-500">Loading 14 days of geographic demand…</p>
    </div>
  );
}

function ErrorState({ error, onRetry }: { error: string; onRetry: () => void }) {
  return (
    <div className="mt-6 rounded-xl border border-red-200 bg-red-50 p-6 text-center" role="alert">
      <h2 className="font-semibold text-red-900">Couldn&apos;t load geographic demand</h2>
      <p className="mt-1 text-sm text-red-700">{error}</p>
      <button
        type="button"
        className="mt-4 rounded-md bg-red-700 px-3.5 py-2 text-sm font-medium text-white hover:bg-red-800"
        onClick={onRetry}
      >
        Retry
      </button>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="mt-6 rounded-xl border border-gray-200 bg-gray-50 p-8 text-center">
      <h2 className="font-semibold text-gray-900">No request origins yet</h2>
      <p className="mt-1 text-sm text-gray-500">
        There were no requests in the latest 14-day analytics window.
      </p>
    </div>
  );
}

function SelectionPanel({
  data,
  atlas,
  hourIndex,
  selection,
  announce,
}: {
  data: GeoAnalyticsResponse;
  atlas: PreparedAtlas;
  hourIndex: number;
  selection: GlobeSelection | null;
  announce: boolean;
}) {
  if (!selection) {
    return (
      <section
        aria-live={announce ? 'polite' : 'off'}
        className="rounded-xl border border-white/10 bg-[#0d111a]/95 p-3"
      >
        <p className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">Selection</p>
        <h3 className="mt-1 text-sm font-semibold text-white">Explore origin demand</h3>
        <p className="mt-1 text-xs text-gray-400">Select a request-origin dot on the globe.</p>
      </section>
    );
  }

  const bucketIndex = buildColumnIndex(data.bucket_cols);
  const hour = data.hours[hourIndex];
  const rows = (hour?.b ?? []).filter(
    (row) => String(row[bucketIndex.c] ?? '') === selection.country,
  );
  const requests = rows.reduce((sum, row) => sum + Number(row[bucketIndex.n] ?? 0), 0);
  const outputTokens = rows.reduce((sum, row) => sum + Number(row[bucketIndex.tout] ?? 0), 0);
  const continent = String(rows[0]?.[bucketIndex.cont] ?? '?');
  const coordinate = atlas.coordinates.get(selection.country);
  const selectedTime = new Date(data.hours_index[hourIndex]);
  const localHour =
    coordinate && !Number.isNaN(selectedTime.getTime())
      ? (selectedTime.getUTCHours() + Math.round(coordinate[0] / 15) + 24) % 24
      : null;
  return (
    <section
      aria-live={announce ? 'polite' : 'off'}
      className="rounded-xl border border-white/10 bg-[#0d111a]/95 p-3"
    >
      <p className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
        Request origin (IP-based)
      </p>
      <h3 className="mt-1 text-sm font-semibold text-white">
        {atlasCountryName(atlas, selection.country)} · {CONTINENT_NAMES[continent] ?? '?'}
      </h3>
      <div className="mt-2 space-y-1 text-xs text-gray-300">
        <p>
          {formatCount(requests)} requests this hour
          {localHour === null ? '' : ` · ~${String(localHour).padStart(2, '0')}:00 local`}
        </p>
        <p>{formatCount(outputTokens)} output tokens</p>
      </div>
    </section>
  );
}

function GeoDashboard({ data, atlas }: { data: GeoAnalyticsResponse; atlas: PreparedAtlas }) {
  const [hourIndex, setHourIndex] = useState(Math.max(0, data.hours_index.length - 1));
  const [metric, setMetric] = useState<GeoMetric>('n');
  const [playing, setPlaying] = useState(false);
  const [selection, setSelection] = useState<GlobeSelection | null>(null);
  const [viewRequest, setViewRequest] = useState<GlobeViewRequest>({
    id: 0,
    rotation: viewPresets[0].rotation,
  });

  useEffect(() => {
    if (!playing) return;
    const interval = window.setInterval(() => {
      setHourIndex((current) => (current + 1 >= data.hours_index.length ? 0 : current + 1));
    }, 150);
    return () => window.clearInterval(interval);
  }, [data.hours_index.length, playing]);

  const setBoundedHour = useCallback(
    (next: number) => {
      setHourIndex(Math.max(0, Math.min(data.hours_index.length - 1, next)));
    },
    [data.hours_index.length],
  );
  const handleSelect = useCallback((next: GlobeSelection) => setSelection(next), []);
  const stats = useMemo(() => currentHourStats(data, hourIndex, metric), [data, hourIndex, metric]);

  const age = formatAge(data.meta.generated_at);
  const unplotted = useMemo(
    () =>
      [...buildCountryContinentMap(data).keys()].filter(
        (country) => !country.startsWith('?') && !atlas.coordinates.has(country),
      ),
    [atlas.coordinates, data],
  );
  const showAttribution =
    data.meta.source !== 'synthetic-demo' &&
    data.meta.geoip?.provider === 'dbip-lite' &&
    data.meta.geoip.attribution !== null;

  return (
    <div className="mt-6 space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            aria-pressed={playing}
            className={`rounded-md px-3 py-1.5 text-sm font-medium transition ${
              playing ? 'bg-blue-600 text-white' : 'border border-gray-200 bg-white text-gray-700'
            }`}
            onClick={() => setPlaying((current) => !current)}
          >
            {playing ? '⏸ Pause' : '▶ Play'}
          </button>
          <button
            type="button"
            className="rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-600 disabled:opacity-40"
            disabled={hourIndex === 0}
            onClick={() => setBoundedHour(hourIndex - 24)}
          >
            −24h
          </button>
          <button
            type="button"
            className="rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-600 disabled:opacity-40"
            disabled={hourIndex === data.hours_index.length - 1}
            onClick={() => setBoundedHour(hourIndex + 24)}
          >
            +24h
          </button>
          <label className="ml-1 flex flex-col gap-0.5 text-[10px] font-semibold uppercase tracking-wide text-gray-500">
            Metric
            <select
              aria-label="Metric"
              className="rounded-md border border-gray-200 bg-white px-2 py-1.5 text-xs font-normal normal-case tracking-normal text-gray-700"
              onChange={(event) => setMetric(event.target.value as GeoMetric)}
              value={metric}
            >
              {metricOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <div className="ml-1">
            <p className="mb-0.5 text-[10px] font-semibold uppercase tracking-wide text-gray-500">
              View
            </p>
            <div className="inline-flex overflow-hidden rounded-md border border-gray-200">
              {viewPresets.map((preset) => (
                <button
                  key={preset.label}
                  type="button"
                  className="border-r border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-600 last:border-r-0 hover:bg-gray-50"
                  onClick={() =>
                    setViewRequest((current) => ({
                      id: current.id + 1,
                      rotation: preset.rotation,
                    }))
                  }
                >
                  {preset.label}
                </button>
              ))}
            </div>
          </div>
        </div>
        <div aria-live={playing ? 'off' : 'polite'} className="text-right">
          <div className="flex items-center justify-end gap-2">
            {data.meta.source === 'synthetic-demo' && (
              <span className="rounded bg-amber-400 px-2 py-0.5 text-[10px] font-bold tracking-wide text-amber-950">
                SYNTHETIC DEMO
              </span>
            )}
            {age.stale && (
              <span className="rounded bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-800">
                STALE
              </span>
            )}
            <p className="font-semibold text-gray-900">
              {formatTimestamp(data.hours_index[hourIndex])}
            </p>
          </div>
          <p className="mt-0.5 text-xs text-gray-500">Hourly origin demand · {age.label}</p>
        </div>
      </div>

      {(data.meta.degraded || !data.meta.geoip?.country) && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          Geography is degraded; unlocated traffic remains in totals but is not plotted.
          {data.meta.degraded_reasons.length > 0 && (
            <span className="ml-1 text-xs text-amber-700">
              ({data.meta.degraded_reasons.join(', ')})
            </span>
          )}
        </div>
      )}

      <GeoStatCards stats={stats} />

      <div className="grid min-w-0 gap-3 lg:grid-cols-[minmax(0,1fr)_15rem]">
        <GlobeCanvas
          atlas={atlas}
          data={data}
          hourIndex={hourIndex}
          metric={metric}
          onSelect={handleSelect}
          viewRequest={viewRequest}
        />
        <aside className="grid content-start gap-3 sm:grid-cols-2 lg:grid-cols-1">
          <SelectionPanel
            announce={!playing}
            atlas={atlas}
            data={data}
            hourIndex={hourIndex}
            selection={selection}
          />
        </aside>
      </div>

      <TrafficRibbon
        data={data}
        hourIndex={hourIndex}
        metric={metric}
        onHourChange={setBoundedHour}
      />

      <footer className="space-y-1 px-1 text-[11px] leading-relaxed text-gray-500">
        <p>
          Source: <strong className="font-medium text-gray-700">{data.meta.source}</strong> ·{' '}
          {formatCount(data.meta.rows_total)} requests over {data.hours_index.length} hours ·{' '}
          {age.label}
        </p>
        <p>
          GeoIP country: {String(data.meta.geoip?.country ?? false)}
          {showAttribution && (
            <>
              {' · '}
              <a
                className="underline decoration-gray-300 underline-offset-2 hover:text-gray-800"
                href="https://db-ip.com"
                rel="noopener noreferrer"
                target="_blank"
              >
                {data.meta.geoip.attribution?.label ?? 'IP Geolocation by DB-IP'}
              </a>
            </>
          )}
          {unplotted.length > 0 && ` · not plotted (no centroid): ${unplotted.join(', ')}`}
          {data.meta.unmapped_alpha2.length > 0 &&
            ` · unmapped alpha-2: ${data.meta.unmapped_alpha2.join(', ')}`}
        </p>
        <p>
          Origin = network origin (IP-based), not residence · country local times are approximate
          (longitude-based)
        </p>
      </footer>
    </div>
  );
}

export function GeoGlobe() {
  const [attempt, setAttempt] = useState(0);
  const [data, setData] = useState<GeoAnalyticsResponse | null>(null);
  const [atlas, setAtlas] = useState<PreparedAtlas | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    Promise.all([
      getGeoAnalytics({ days: 14, signal: controller.signal }),
      fetch('/atlas/countries-110m.json', { signal: controller.signal }).then(async (response) => {
        if (!response.ok) throw new Error(`World atlas failed to load (HTTP ${response.status})`);
        return prepareAtlas(await response.json());
      }),
    ])
      .then(([nextData, nextAtlas]) => {
        if (controller.signal.aborted) return;
        setData(nextData);
        setAtlas(nextAtlas);
        setLoading(false);
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setError(getErrorMessage(reason));
        setLoading(false);
      });
    return () => controller.abort();
  }, [attempt]);

  if (loading) return <LoadingState />;
  if (error) return <ErrorState error={error} onRetry={() => setAttempt((value) => value + 1)} />;
  if (!data || !atlas)
    return (
      <ErrorState
        error="No analytics response was returned."
        onRetry={() => setAttempt((value) => value + 1)}
      />
    );
  if (!data.hours_index.length || !data.hours.length || data.meta.rows_total === 0) {
    return <EmptyState />;
  }
  return <GeoDashboard atlas={atlas} data={data} />;
}
