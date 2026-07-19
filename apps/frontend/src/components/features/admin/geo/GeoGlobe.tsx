'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { GeoAnalyticsResponse, GeoMetric } from '@/lib/api/admin';
import { getGeoAnalytics } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { DataDetailsDialog } from './DataDetailsDialog';
import {
  atlasCountryName,
  GlobeCanvas,
  prepareAtlas,
  type GlobeSelection,
  type GlobeViewRequest,
  type PreparedAtlas,
} from './GlobeCanvas';
import {
  demandWeightedRotation,
  deriveGeoMetricModel,
  hourOriginSummary,
  rotationForCoordinate,
} from './geoMath';
import { TopOrigins } from './TopOrigins';
import { WindowTimeline } from './WindowTimeline';

const HOUR_MS = 3_600_000;
const STALE_AFTER_MS = 2 * HOUR_MS;
const DEFAULT_ROTATION: [number, number] = [-100, -28];

export type GeoRangeDays = 7 | 14 | 30;

const METRIC_OPTIONS: { value: GeoMetric; label: string }[] = [
  { value: 'n', label: 'Requests' },
  { value: 'tout', label: 'Tokens' },
];

const RANGE_OPTIONS: { value: GeoRangeDays; label: string }[] = [
  { value: 7, label: '7d' },
  { value: 14, label: '14d' },
  { value: 30, label: '30d' },
];

function formatCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 10_000) return `${Math.round(value / 1_000)}k`;
  return Math.round(value).toLocaleString('en-US');
}

function metricUnit(metric: GeoMetric): string {
  return metric === 'tout' ? 'output tokens' : 'requests';
}

function hourHeadline(timestamp: string | undefined): string {
  const date = new Date(timestamp ?? '');
  if (Number.isNaN(date.getTime())) return 'unknown hour';
  const day = date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
  return `at ${String(date.getUTCHours()).padStart(2, '0')}:00 UTC · ${day}`;
}

function hourShortLabel(timestamp: string | undefined): string {
  const date = new Date(timestamp ?? '');
  if (Number.isNaN(date.getTime())) return '--:00 UTC';
  return `${String(date.getUTCHours()).padStart(2, '0')}:00 UTC`;
}

function isStale(generatedAt: string): boolean {
  const generated = new Date(generatedAt).getTime();
  if (!Number.isFinite(generated)) return true;
  return Date.now() - generated > STALE_AFTER_MS;
}

function Segmented<Value extends string | number>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: readonly { value: Value; label: string }[];
  value: Value;
  onChange: (next: Value) => void;
}) {
  return (
    <div
      aria-label={label}
      className="inline-flex overflow-hidden rounded-lg border border-gray-200 bg-white"
      role="group"
    >
      {options.map((option) => {
        const active = option.value === value;
        return (
          <button
            key={String(option.value)}
            type="button"
            aria-pressed={active}
            className={`px-3 py-1.5 text-[13px] font-medium transition ${
              active ? 'bg-gray-900 text-white' : 'text-gray-600 hover:bg-gray-50'
            }`}
            onClick={() => onChange(option.value)}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

async function loadAtlas(signal: AbortSignal): Promise<PreparedAtlas> {
  const response = await fetch('/atlas/countries-110m.json', { signal });
  if (!response.ok) throw new Error(`World atlas failed to load (HTTP ${response.status})`);
  return prepareAtlas(await response.json());
}

function LoadingState() {
  return (
    <div aria-label="Loading request origins" className="mt-6 space-y-3" role="status">
      <div className="h-16 animate-pulse rounded-xl bg-gray-100" />
      <div className="h-[440px] animate-pulse rounded-xl bg-gray-900" />
      <p className="text-center text-sm text-gray-500">Loading request origins…</p>
    </div>
  );
}

function ErrorState({ error, onRetry }: { error: string; onRetry: () => void }) {
  return (
    <div className="mt-6 rounded-xl border border-red-200 bg-red-50 p-6 text-center" role="alert">
      <h2 className="font-semibold text-red-900">Couldn&apos;t load request origins</h2>
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

function EmptyState({
  days,
  onDaysChange,
}: {
  days: GeoRangeDays;
  onDaysChange: (next: GeoRangeDays) => void;
}) {
  return (
    <div className="mt-6 rounded-xl border border-gray-200 bg-gray-50 p-8 text-center">
      <h2 className="font-semibold text-gray-900">No request origins yet</h2>
      <p className="mt-1 text-sm text-gray-500">
        There were no requests in the latest {days}-day window.
      </p>
      <div className="mt-4 flex justify-center">
        <Segmented label="Range" onChange={onDaysChange} options={RANGE_OPTIONS} value={days} />
      </div>
      <p className="mt-2 text-xs text-gray-400">Try a longer range.</p>
    </div>
  );
}

function Hero({
  data,
  hourIndex,
  metric,
  summary,
  atlas,
}: {
  data: GeoAnalyticsResponse;
  hourIndex: number;
  metric: GeoMetric;
  summary: ReturnType<typeof hourOriginSummary>;
  atlas: PreparedAtlas;
}) {
  const unit = metricUnit(metric);
  const countryWord = summary.activeCountries === 1 ? 'country' : 'countries';
  let subline = 'No requests recorded in this hour.';
  if (summary.totalRequests > 0 && summary.top === null) {
    subline = 'Origins unknown for all requests this hour.';
  } else if (summary.top !== null) {
    const topName = atlasCountryName(atlas, summary.top.country);
    const topShare = Math.round(summary.topShare * 100);
    subline = `${topName} ${topShare}% · ${summary.activeCountries} active ${countryWord}`;
  }
  return (
    <div>
      <p className="text-[28px] font-bold leading-tight tracking-tight text-gray-900">
        <span>
          {summary.totalValue > 0 ? `${formatCount(summary.totalValue)} ${unit}` : `No ${unit}`}
        </span>
        <span className="ml-2 text-base font-medium text-gray-500">
          {hourHeadline(data.hours_index[hourIndex])}
        </span>
      </p>
      <p aria-live="polite" className="mt-0.5 text-sm text-gray-600">
        {subline}
      </p>
    </div>
  );
}

function GeoDashboard({
  data,
  atlas,
  days,
  onDaysChange,
}: {
  data: GeoAnalyticsResponse;
  atlas: PreparedAtlas;
  days: GeoRangeDays;
  onDaysChange: (next: GeoRangeDays) => void;
}) {
  const lastIndex = Math.max(0, data.hours_index.length - 1);
  const [hourIndex, setHourIndex] = useState(lastIndex);
  const [metric, setMetric] = useState<GeoMetric>('n');
  const [playing, setPlaying] = useState(false);
  const [selection, setSelection] = useState<string | null>(null);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const initialRotation = useMemo(
    () =>
      demandWeightedRotation(data, atlas.coordinates, lastIndex) ??
      demandWeightedRotation(data, atlas.coordinates) ??
      DEFAULT_ROTATION,
    [atlas.coordinates, data, lastIndex],
  );
  const [viewRequest, setViewRequest] = useState<GlobeViewRequest>({
    id: 0,
    rotation: initialRotation,
  });
  const safeHourIndex = Math.min(hourIndex, lastIndex);

  // A cached range switch swaps `data` without remounting (the key is the range,
  // and the dashboard first mounts with the previous payload). Re-anchor the
  // hour, selection, and camera whenever the payload identity changes.
  const dataRef = useRef(data);
  useEffect(() => {
    if (dataRef.current === data) return;
    dataRef.current = data;
    const last = Math.max(0, data.hours_index.length - 1);
    setHourIndex(last);
    setSelection(null);
    setViewRequest((request) => ({
      id: request.id + 1,
      rotation:
        demandWeightedRotation(data, atlas.coordinates, last) ??
        demandWeightedRotation(data, atlas.coordinates) ??
        DEFAULT_ROTATION,
    }));
  }, [atlas.coordinates, data]);

  useEffect(() => {
    if (!playing) return;
    const interval = window.setInterval(() => {
      setHourIndex((current) => (current + 1 > lastIndex ? 0 : current + 1));
    }, 150);
    return () => window.clearInterval(interval);
  }, [lastIndex, playing]);

  const setBoundedHour = useCallback(
    (next: number) => setHourIndex(Math.max(0, Math.min(lastIndex, next))),
    [lastIndex],
  );
  const selectionRef = useRef<string | null>(null);
  useEffect(() => {
    selectionRef.current = selection;
  }, [selection]);
  const handleGlobeSelect = useCallback((next: GlobeSelection) => {
    setSelection((current) => (current === next.country ? null : next.country));
  }, []);
  const handleOriginSelect = useCallback(
    (country: string) => {
      if (selectionRef.current === country) {
        setSelection(null);
        return;
      }
      setSelection(country);
      const coordinate = atlas.coordinates.get(country);
      if (coordinate) {
        setViewRequest((request) => ({
          id: request.id + 1,
          rotation: rotationForCoordinate(coordinate),
          animate: true,
        }));
      }
    },
    [atlas.coordinates],
  );

  const closeDetails = useCallback(() => setDetailsOpen(false), []);
  const metricModel = useMemo(() => deriveGeoMetricModel(data, metric), [data, metric]);
  const summary = useMemo(
    () => hourOriginSummary(data, safeHourIndex, metricModel),
    [data, safeHourIndex, metricModel],
  );
  const stale = isStale(data.meta.generated_at);

  return (
    <div className="mt-6 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <Segmented label="Metric" onChange={setMetric} options={METRIC_OPTIONS} value={metric} />
          <Segmented label="Range" onChange={onDaysChange} options={RANGE_OPTIONS} value={days} />
        </div>
        <div className="flex items-center gap-2">
          {data.meta.source === 'synthetic-demo' && (
            <span className="rounded bg-amber-400 px-2 py-0.5 text-[10px] font-bold tracking-wide text-amber-950">
              SYNTHETIC DEMO
            </span>
          )}
          {stale && (
            <span className="rounded bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-800">
              STALE
            </span>
          )}
        </div>
      </div>

      <Hero atlas={atlas} data={data} hourIndex={safeHourIndex} metric={metric} summary={summary} />

      {(data.meta.degraded || !data.meta.geoip.country) && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          Location data is degraded — unlocated traffic stays in the totals but is not plotted.
        </div>
      )}

      <div className="grid min-w-0 items-start gap-3 lg:grid-cols-[minmax(0,1fr)_15rem]">
        <GlobeCanvas
          atlas={atlas}
          data={data}
          hourIndex={safeHourIndex}
          metricModel={metricModel}
          onSelect={handleGlobeSelect}
          selectedCountry={selection}
          viewRequest={viewRequest}
        />
        <TopOrigins
          announce={!playing}
          atlas={atlas}
          hourLabel={hourShortLabel(data.hours_index[safeHourIndex])}
          metric={metric}
          onSelect={handleOriginSelect}
          selectedCountry={selection}
          summary={summary}
        />
      </div>

      <WindowTimeline
        data={data}
        hourIndex={safeHourIndex}
        metricModel={metricModel}
        onHourChange={setBoundedHour}
        onTogglePlay={() => setPlaying((current) => !current)}
        playing={playing}
      />

      <footer className="flex flex-wrap items-center gap-x-2 gap-y-1 px-1 text-[13px] text-gray-500">
        <span>Locations are estimated from request IPs.</span>
        <button
          type="button"
          className="font-medium text-blue-700 underline decoration-blue-200 underline-offset-2 hover:text-blue-900"
          onClick={() => setDetailsOpen(true)}
        >
          Data details
        </button>
      </footer>

      <DataDetailsDialog
        data={data}
        metricModel={metricModel}
        onClose={closeDetails}
        open={detailsOpen}
      />
    </div>
  );
}

export function GeoGlobe() {
  const [attempt, setAttempt] = useState(0);
  const [days, setDays] = useState<GeoRangeDays>(14);
  const [data, setData] = useState<GeoAnalyticsResponse | null>(null);
  const [atlas, setAtlas] = useState<PreparedAtlas | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const cacheRef = useRef(new Map<GeoRangeDays, GeoAnalyticsResponse>());

  useEffect(() => {
    const cached = cacheRef.current.get(days);
    if (cached && atlas) {
      setData(cached);
      setLoading(false);
      setError(null);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    Promise.all([
      getGeoAnalytics({ days, signal: controller.signal }),
      atlas ? Promise.resolve(atlas) : loadAtlas(controller.signal),
    ])
      .then(([nextData, nextAtlas]) => {
        if (controller.signal.aborted) return;
        cacheRef.current.set(days, nextData);
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
    // `atlas` is intentionally read but not depended on: it never changes once loaded.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attempt, days]);

  if (loading && !data) return <LoadingState />;
  if (error) return <ErrorState error={error} onRetry={() => setAttempt((value) => value + 1)} />;
  if (!data || !atlas)
    return (
      <ErrorState
        error="No analytics response was returned."
        onRetry={() => setAttempt((value) => value + 1)}
      />
    );
  if (loading) return <LoadingState />;
  if (!data.hours_index.length || !data.hours.length || data.meta.rows_total === 0) {
    return <EmptyState days={days} onDaysChange={setDays} />;
  }
  return <GeoDashboard key={days} atlas={atlas} data={data} days={days} onDaysChange={setDays} />;
}
