'use client';

import { useQuery } from '@tanstack/react-query';
import type { ReactNode } from 'react';
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
  deriveGeoMetricModel,
  hourOriginSummary,
  landPoints,
  openingRotation,
  ORIGIN_COLORS,
  rotationForCoordinate,
  windowOriginSummary,
} from './geoMath';
import { TopOrigins } from './TopOrigins';
import { WindowTimeline } from './WindowTimeline';

const HOUR_MS = 3_600_000;
const STALE_AFTER_MS = 2 * HOUR_MS;
const GEO_QUERY_STALE_MS = 60_000;
const GEO_QUERY_REFRESH_MS = 5 * 60_000;
const DEFAULT_ROTATION: [number, number] = [-20, -30];
// Spacing of the land dot matrix, in degrees of latitude.
const LAND_STEP_DEGREES = 1.45;

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
  if (value >= 10_000) return `${(value / 1_000).toFixed(1)}k`;
  return Math.round(value).toLocaleString('en-US');
}

function metricUnit(metric: GeoMetric): string {
  return metric === 'tout' ? 'output tokens' : 'requests';
}

function hourHeadline(timestamp: string | undefined): string {
  const date = new Date(timestamp ?? '');
  if (Number.isNaN(date.getTime())) return 'Unknown hour';
  const day = date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
  return `Hour of ${day} · ${String(date.getUTCHours()).padStart(2, '0')}:00 UTC`;
}

function hourShortLabel(timestamp: string | undefined): string {
  const date = new Date(timestamp ?? '');
  if (Number.isNaN(date.getTime())) return '--:00 UTC';
  return `${String(date.getUTCHours()).padStart(2, '0')}:00 UTC`;
}

function windowHeadline(days: GeoRangeDays, timestamp: string | undefined): string {
  const date = new Date(timestamp ?? '');
  if (Number.isNaN(date.getTime())) return `Last ${days} days`;
  const day = date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
  return `Last ${days} days · through ${day} ${String(date.getUTCHours()).padStart(2, '0')}:00 UTC`;
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
  tone = 'dark',
}: {
  label: string;
  options: readonly { value: Value; label: string }[];
  value: Value;
  onChange: (next: Value) => void;
  tone?: 'light' | 'dark';
}) {
  const dark = tone === 'dark';
  return (
    <div
      aria-label={label}
      className={`inline-flex rounded-lg p-0.5 ${
        dark ? 'bg-white/[0.06] ring-1 ring-inset ring-white/10' : 'bg-gray-100'
      }`}
      role="group"
    >
      {options.map((option) => {
        const active = option.value === value;
        const stateClass = dark
          ? active
            ? 'bg-white/15 text-white'
            : 'text-slate-400 hover:text-slate-200'
          : active
            ? 'bg-white text-gray-900 shadow-sm'
            : 'text-gray-500 hover:text-gray-800';
        return (
          <button
            key={String(option.value)}
            type="button"
            aria-pressed={active}
            className={`rounded-md px-2.5 py-1 text-[13px] font-medium transition focus:outline-none focus-visible:ring-2 sm:px-3 ${
              dark ? 'focus-visible:ring-sky-300/80' : 'focus-visible:ring-blue-600'
            } ${stateClass}`}
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
    <div
      aria-label="Loading request origins"
      className="mt-6 flex h-[560px] items-center justify-center rounded-2xl"
      role="status"
      style={{ backgroundColor: ORIGIN_COLORS.panel }}
    >
      <div className="flex flex-col items-center gap-4">
        <div className="h-48 w-48 animate-pulse rounded-full bg-white/[0.05] ring-1 ring-sky-300/10" />
        <p className="text-sm text-slate-500">Loading request origins…</p>
      </div>
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
        <Segmented
          label="Range"
          onChange={onDaysChange}
          options={RANGE_OPTIONS}
          tone="light"
          value={days}
        />
      </div>
      <p className="mt-2 text-xs text-gray-400">Try a longer range.</p>
    </div>
  );
}

function Hero({
  mode,
  days,
  metric,
  summary,
  atlas,
}: {
  mode: 'window' | 'hour';
  days: GeoRangeDays;
  metric: GeoMetric;
  summary: ReturnType<typeof windowOriginSummary>;
  atlas: PreparedAtlas;
}) {
  const unit = metricUnit(metric);
  const countryWord = summary.activeCountries === 1 ? 'country' : 'countries';
  const scope = mode === 'window' ? `this ${days}-day window` : 'this hour';
  let subline: ReactNode = `No requests recorded in ${scope}.`;
  if (summary.totalRequests > 0 && summary.top === null) {
    subline = `Origins unknown for all requests in ${scope}.`;
  } else if (summary.top !== null) {
    subline = (
      <>
        <span className="text-white">{atlasCountryName(atlas, summary.top.country)}</span> leads
        with {Math.round(summary.topShare * 100)}% of all {unit}.
      </>
    );
  }
  return (
    <div className="min-w-0">
      <p
        className="text-[44px] font-light leading-none tracking-tight text-white tabular-nums"
        data-testid="origins-total"
      >
        {formatCount(summary.totalValue)}
      </p>
      <p className="mt-2 text-sm text-slate-400">
        {summary.activeCountries > 0
          ? `${unit} from ${summary.activeCountries} ${countryWord}`
          : unit}
      </p>
      <p aria-live="polite" className="mt-4 text-[15px] leading-snug text-slate-300">
        {subline}
      </p>
    </div>
  );
}

/** The opening view: always of request volume, so a metric switch never turns the globe. */
function openingView(
  data: GeoAnalyticsResponse,
  atlas: PreparedAtlas,
  land: readonly [number, number][],
): [number, number] {
  const origins = windowOriginSummary(data, deriveGeoMetricModel(data, 'n')).origins;
  return openingRotation(origins, atlas.coordinates, land) ?? DEFAULT_ROTATION;
}

function GeoDashboard({
  data,
  atlas,
  land,
  days,
  onDaysChange,
}: {
  data: GeoAnalyticsResponse;
  atlas: PreparedAtlas;
  land: readonly [number, number][];
  days: GeoRangeDays;
  onDaysChange: (next: GeoRangeDays) => void;
}) {
  const lastIndex = Math.max(0, data.hours_index.length - 1);
  const [selectedHourIndex, setSelectedHourIndex] = useState<number | null>(null);
  const [metric, setMetric] = useState<GeoMetric>('n');
  const [playing, setPlaying] = useState(false);
  const [selection, setSelection] = useState<string | null>(null);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const initialRotation = useMemo(() => openingView(data, atlas, land), [atlas, data, land]);
  const [viewRequest, setViewRequest] = useState<GlobeViewRequest>({
    id: 0,
    rotation: initialRotation,
  });
  const safeHourIndex = Math.max(0, Math.min(selectedHourIndex ?? lastIndex, lastIndex));
  const mode = selectedHourIndex === null ? 'window' : 'hour';

  // A cached range switch swaps `data` without remounting. Reset to the range
  // overview rather than carrying a drilled-down hour into a different window.
  const dataRef = useRef(data);
  useEffect(() => {
    if (dataRef.current === data) return;
    dataRef.current = data;
    setSelectedHourIndex(null);
    setPlaying(false);
    setSelection(null);
    setViewRequest((request) => ({ id: request.id + 1, rotation: openingView(data, atlas, land) }));
  }, [atlas, data, land]);

  useEffect(() => {
    if (!playing) return;
    const interval = window.setInterval(() => {
      setSelectedHourIndex((current) => {
        const next = current ?? 0;
        return next + 1 > lastIndex ? 0 : next + 1;
      });
    }, 150);
    return () => window.clearInterval(interval);
  }, [lastIndex, playing]);

  const setBoundedHour = useCallback(
    (next: number) => setSelectedHourIndex(Math.max(0, Math.min(lastIndex, next))),
    [lastIndex],
  );
  const togglePlay = useCallback(() => {
    setPlaying((current) => !current);
    setSelectedHourIndex((current) => current ?? 0);
  }, []);
  const returnToWindowTotal = useCallback(() => {
    setPlaying(false);
    setSelectedHourIndex(null);
    setSelection(null);
  }, []);
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
          unlessVisible: coordinate,
        }));
      }
    },
    [atlas.coordinates],
  );

  const closeDetails = useCallback(() => setDetailsOpen(false), []);
  const metricModel = useMemo(() => deriveGeoMetricModel(data, metric), [data, metric]);
  const summary = useMemo(
    () =>
      mode === 'hour'
        ? hourOriginSummary(data, safeHourIndex, metricModel)
        : windowOriginSummary(data, metricModel),
    [data, metricModel, mode, safeHourIndex],
  );
  const stale = isStale(data.meta.generated_at);

  return (
    <div className="mt-6">
      {/* One composed night panel: numbers, globe and timeline are a single
          view of one selection, not separate cards. */}
      <section
        aria-label="Request origins overview"
        className="overflow-hidden rounded-2xl text-slate-300 shadow-sm ring-1 ring-black/5"
        style={{ backgroundColor: ORIGIN_COLORS.panel }}
      >
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/[0.07] px-5 py-3.5 sm:px-8">
          <div className="flex flex-wrap items-center gap-2.5">
            <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-slate-400">
              {mode === 'window'
                ? windowHeadline(days, data.hours_index[lastIndex])
                : hourHeadline(data.hours_index[safeHourIndex])}
            </p>
            {data.meta.source === 'synthetic-demo' && (
              <span className="rounded bg-amber-400 px-1.5 py-0.5 text-[10px] font-bold tracking-wide text-amber-950">
                SYNTHETIC DEMO
              </span>
            )}
            {stale && (
              <span className="rounded bg-amber-300/15 px-1.5 py-0.5 text-[10px] font-semibold text-amber-200 ring-1 ring-inset ring-amber-300/30">
                STALE
              </span>
            )}
          </div>
          <div className="flex flex-wrap gap-2">
            <Segmented
              label="Metric"
              onChange={setMetric}
              options={METRIC_OPTIONS}
              value={metric}
            />
            <Segmented label="Range" onChange={onDaysChange} options={RANGE_OPTIONS} value={days} />
          </div>
        </div>

        {(data.meta.degraded || !data.meta.geoip.country) && (
          <div className="mx-5 mt-4 rounded-lg bg-amber-300/10 px-3 py-2 text-sm text-amber-200 ring-1 ring-inset ring-amber-300/25 sm:mx-8">
            Location data is degraded — unlocated traffic stays in the totals but is not plotted.
          </div>
        )}

        {/* Phones read hero, globe, then the list; wide screens put the globe
            beside both. */}
        <div className="grid lg:grid-cols-[minmax(0,19.5rem)_minmax(0,1fr)] lg:grid-rows-[auto_1fr]">
          <div className="px-5 pt-6 sm:px-8 lg:col-start-1 lg:row-start-1 lg:pt-8">
            <Hero atlas={atlas} days={days} metric={metric} mode={mode} summary={summary} />
          </div>
          <div className="h-[clamp(320px,100vw,460px)] min-w-0 lg:col-start-2 lg:row-span-2 lg:row-start-1 lg:h-auto lg:min-h-[540px]">
            <GlobeCanvas
              atlas={atlas}
              land={land}
              metricModel={metricModel}
              mode={mode}
              onSelect={handleGlobeSelect}
              selectedCountry={selection}
              snapshotTimestamp={data.hours_index[safeHourIndex]}
              summary={summary}
              viewRequest={viewRequest}
            />
          </div>
          <div className="px-5 pb-6 sm:px-8 lg:col-start-1 lg:row-start-2 lg:pb-8 lg:pt-6">
            <TopOrigins
              announce={!playing}
              atlas={atlas}
              label={
                mode === 'window'
                  ? `last ${days} days`
                  : hourShortLabel(data.hours_index[safeHourIndex])
              }
              metric={metric}
              mode={mode}
              onSelect={handleOriginSelect}
              selectedCountry={selection}
              summary={summary}
            />
          </div>
        </div>

        <WindowTimeline
          data={data}
          metricModel={metricModel}
          onHourChange={setBoundedHour}
          onReturnToTotal={returnToWindowTotal}
          onTogglePlay={togglePlay}
          playing={playing}
          selectedCountry={selection}
          selectedCountryName={selection ? atlasCountryName(atlas, selection) : null}
          selectedHourIndex={selectedHourIndex}
          totalLabel={`${days}-day total`}
        />

        <footer className="flex flex-wrap items-center gap-x-2 gap-y-1 border-t border-white/[0.07] px-5 py-3 text-xs text-slate-500 sm:px-8">
          <span>Locations are estimated from request IPs.</span>
          <button
            type="button"
            className="rounded font-medium text-sky-300 underline decoration-sky-300/30 underline-offset-2 hover:text-sky-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/80"
            onClick={() => setDetailsOpen(true)}
          >
            Data details
          </button>
        </footer>
      </section>

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
  const [days, setDays] = useState<GeoRangeDays>(14);
  const geoQuery = useQuery({
    queryKey: ['admin', 'analytics', 'geo', days],
    queryFn: ({ signal }) => getGeoAnalytics({ days, signal }),
    staleTime: GEO_QUERY_STALE_MS,
    refetchInterval: GEO_QUERY_REFRESH_MS,
    retry: false,
  });
  const atlasQuery = useQuery({
    queryKey: ['atlas', 'countries-110m'],
    queryFn: ({ signal }) => loadAtlas(signal),
    staleTime: Infinity,
    gcTime: Infinity,
    retry: false,
  });
  const data = geoQuery.data ?? null;
  const atlas = atlasQuery.data ?? null;
  // Sampled once per atlas (about 0.2s), not per range: the dashboard remounts on a range switch.
  const land = useMemo(
    () => (atlas ? landPoints(atlas.countries, LAND_STEP_DEGREES) : []),
    [atlas],
  );
  const error = geoQuery.error ?? atlasQuery.error;

  if ((!data || !atlas) && (geoQuery.isPending || atlasQuery.isPending)) return <LoadingState />;
  if (error && (!data || !atlas)) {
    return (
      <ErrorState
        error={getErrorMessage(error)}
        onRetry={() => {
          if (!data) void geoQuery.refetch();
          if (!atlas) void atlasQuery.refetch();
        }}
      />
    );
  }
  if (!data || !atlas)
    return (
      <ErrorState
        error="No analytics response was returned."
        onRetry={() => {
          void geoQuery.refetch();
          void atlasQuery.refetch();
        }}
      />
    );
  if (!data.hours_index.length || !data.hours.length || data.meta.rows_total === 0) {
    return <EmptyState days={days} onDaysChange={setDays} />;
  }
  return (
    <GeoDashboard
      key={days}
      atlas={atlas}
      data={data}
      days={days}
      land={land}
      onDaysChange={setDays}
    />
  );
}
