'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { getRoutewiseDecisions } from '@/lib/api/admin';
import type {
  RoutewiseDecisionBucket,
  RoutewiseDecisionsRange,
  RoutewiseDecisionsResponse,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

interface RoutewiseDecisionsPanelProps {
  modelId: string;
}

const RANGES: { key: RoutewiseDecisionsRange; label: string }[] = [
  { key: '24h', label: '24h' },
  { key: '7d', label: '7d' },
  { key: '30d', label: '30d' },
];

// Full window span (seconds) per range, used to zero-fill bucket gaps so the
// x-axis always covers the whole window even when the backend returns only the
// non-empty buckets.
const RANGE_WINDOW_SECONDS: Record<RoutewiseDecisionsRange, number> = {
  '24h': 24 * 3600,
  '7d': 7 * 24 * 3600,
  '30d': 30 * 24 * 3600,
};

const DISTRIBUTION_COLORS = [
  '#2563eb',
  '#10b981',
  '#f59e0b',
  '#8b5cf6',
  '#14b8a6',
  '#f97316',
  '#ec4899',
  '#64748b',
  '#84cc16',
  '#06b6d4',
  '#dc2626',
  '#a855f7',
] as const;

const ENDPOINT_COLOR_HINTS: Record<string, string> = {
  chutes: '#f59e0b',
  deepinfra: '#2563eb',
  'minimax/highspeed': '#10b981',
  wandb: '#8b5cf6',
  siliconflow: '#14b8a6',
  'atlas-cloud': '#64748b',
  novita: '#ec4899',
};

const AXIS_TICK = { fontSize: 11, fill: '#6b7280' } as const;
const LEGEND_STYLE = { fontSize: 11, color: '#6b7280' } as const;
const GRID_STROKE = '#e5e7eb';

const TOOLTIP_STYLE = {
  fontSize: 12,
  borderRadius: 8,
  border: '1px solid #e5e7eb',
  boxShadow: '0 1px 3px rgba(0,0,0,0.08)',
  background: '#fff',
  padding: '6px 8px',
} as const;

// Order is the legend order.
const TIER_ORDER = ['on_demand', 'quota', 'concurrency'] as const;
const TIER_META: Record<string, { label: string; color: string }> = {
  on_demand: { label: 'on_demand', color: '#3b82f6' },
  quota: { label: 'quota', color: '#f59e0b' },
  concurrency: { label: 'concurrency', color: '#10b981' },
};

// Hedge stack, bottom to top. not_hedged is a receding neutral gray;
// hedged_primary_won a light orange; hedged_backup_won the paper hedge orange.
const HEDGE_SEGMENTS = [
  { key: 'not_hedged', label: 'not hedged', color: '#e5e7eb' },
  { key: 'hedged_primary_won', label: 'hedged · primary won', color: '#f8c471' },
  { key: 'hedged_backup_won', label: 'hedged · backup won', color: '#f28e2b' },
] as const;

// Client-side zero-fill: the backend returns only non-empty buckets, so a
// window with one burst of traffic would otherwise render a single bar
// spanning the whole chart. Generates the complete epoch-aligned series
// (matching the backend's to_timestamp(floor(epoch/bucket)*bucket) alignment)
// from the window start through now, merging server buckets in and filling
// gaps with zero buckets.
export function fillBucketGaps(
  buckets: RoutewiseDecisionBucket[],
  bucketSeconds: number,
  windowSeconds: number,
  nowMs: number,
): RoutewiseDecisionBucket[] {
  if (!Number.isFinite(bucketSeconds) || bucketSeconds <= 0) return buckets;
  const byStart = new Map<number, RoutewiseDecisionBucket>();
  for (const bucket of buckets) {
    const epochMs = Date.parse(bucket.bucket_start);
    if (Number.isNaN(epochMs)) continue;
    byStart.set(Math.floor(epochMs / 1000 / bucketSeconds) * bucketSeconds, bucket);
  }
  const nowSec = Math.floor(nowMs / 1000);
  const startSec = Math.floor((nowSec - windowSeconds) / bucketSeconds) * bucketSeconds;
  const filled: RoutewiseDecisionBucket[] = [];
  for (let t = startSec; t <= nowSec; t += bucketSeconds) {
    filled.push(
      byStart.get(t) ?? {
        bucket_start: new Date(t * 1000).toISOString(),
        counts: {},
        hedge: { not_hedged: 0, hedged_primary_won: 0, hedged_backup_won: 0 },
      },
    );
  }
  return filled;
}

// With a zero-filled axis of ~24-30 categories, showing every tick label turns
// the axis to mush; aim for roughly six labels.
function xTickInterval(categoryCount: number): number {
  return Math.max(0, Math.ceil(categoryCount / 6) - 1);
}

function shortEndpoint(modelId: string, endpoint: string): string {
  let label = endpoint;
  const prefix = `${modelId}:`;
  if (label.startsWith(prefix)) label = label.slice(prefix.length);
  return label.replace(/-api$/, '');
}

function endpointColorKey(endpoint: string): string {
  const routePart = endpoint.includes(':') ? endpoint.slice(endpoint.indexOf(':') + 1) : endpoint;
  const short = routePart.replace(/-api$/, '');
  const openRouterMatch = short.match(/^openrouter\[(.+)\]$/);
  return openRouterMatch?.[1] ?? short;
}

function hashString(value: string): number {
  let hash = 0;
  for (let i = 0; i < value.length; i += 1) {
    hash = (hash * 31 + value.charCodeAt(i)) | 0;
  }
  return Math.abs(hash);
}

function distributionColorForEndpoint(endpoint: string, usedColors: Set<string>): string {
  const key = endpointColorKey(endpoint);
  const hintedColor = ENDPOINT_COLOR_HINTS[key];
  if (hintedColor && !usedColors.has(hintedColor)) {
    usedColors.add(hintedColor);
    return hintedColor;
  }

  const start = hashString(endpoint) % DISTRIBUTION_COLORS.length;
  for (let offset = 0; offset < DISTRIBUTION_COLORS.length; offset += 1) {
    const color = DISTRIBUTION_COLORS[(start + offset) % DISTRIBUTION_COLORS.length];
    if (!usedColors.has(color)) {
      usedColors.add(color);
      return color;
    }
  }

  return DISTRIBUTION_COLORS[start];
}

function tierLabel(type: string): string {
  return TIER_META[type]?.label ?? type;
}

function fmtBucketLabel(iso: string, range: RoutewiseDecisionsRange): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  if (range === '24h') {
    return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  }
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${mm}-${dd}`;
}

function fmtPct(count: number, total: number): string {
  if (!total) return '0%';
  return `${Math.round((count / total) * 100)}%`;
}

// One-decimal percentage for the hedge KPIs (e.g. 0.1875 -> "18.8%").
function fmtRatePct(rate: number | null | undefined): string {
  if (rate == null || !Number.isFinite(rate)) return '—';
  return `${(rate * 100).toFixed(1)}%`;
}

// Tier mix over the window (the paper's provider-mix metric), not
// per-endpoint shares: those are already visible in the chart legend.
function shareSummary(share: RoutewiseDecisionsResponse['selection_share']): string {
  const total = share.reduce((sum, item) => sum + item.count, 0);
  if (!total) return '';
  const byTier = new Map<string, number>();
  for (const item of share) {
    const tier = item.provider_type || 'unknown';
    byTier.set(tier, (byTier.get(tier) ?? 0) + item.count);
  }
  const ordered = [
    ...TIER_ORDER.filter((tier) => byTier.has(tier)),
    ...[...byTier.keys()].filter((tier) => !TIER_ORDER.includes(tier as never)),
  ];
  return ordered
    .map((tier) => `${tierLabel(tier)} ${fmtPct(byTier.get(tier) ?? 0, total)}`)
    .join(' · ');
}

type DistributionSeries = {
  endpoint: string;
  dataKey: string;
  short: string;
  tier: string | null;
  color: string;
};

type DistributionTooltipPayloadEntry = {
  dataKey?: string | number;
  value?: number | string | null;
};

function DistributionTooltip(props: {
  active?: boolean;
  label?: string;
  payload?: DistributionTooltipPayloadEntry[];
  series: DistributionSeries[];
}) {
  const { active, label, payload, series } = props;
  if (!active || !payload || payload.length === 0) return null;
  const metaByKey = new Map(series.map((item) => [item.dataKey, item]));
  const rows = payload
    .map((entry) => {
      const meta = entry.dataKey != null ? metaByKey.get(String(entry.dataKey)) : undefined;
      const value =
        typeof entry.value === 'number' ? entry.value : Number.parseFloat(String(entry.value));
      if (!meta || !Number.isFinite(value) || value <= 0) return null;
      return { meta, value };
    })
    .filter((row): row is { meta: DistributionSeries; value: number } => row != null)
    .sort((a, b) => b.value - a.value);

  if (rows.length === 0) return null;

  return (
    <div style={TOOLTIP_STYLE}>
      {label != null && <div className="font-medium text-gray-900">{label}</div>}
      <div className="mt-1 space-y-1" data-testid="routewise-distribution-tooltip">
        {rows.map(({ meta, value }) => (
          <div
            key={meta.dataKey}
            className="grid grid-cols-[auto_minmax(0,1fr)_auto_auto] items-center gap-x-2 text-gray-600"
          >
            <span
              aria-hidden
              style={{
                display: 'inline-block',
                width: 8,
                height: 8,
                borderRadius: 2,
                background: meta.color,
              }}
            />
            <span className="truncate">{meta.short}</span>
            <span className="text-gray-400">{meta.tier ? tierLabel(meta.tier) : 'unknown'}</span>
            <span className="text-right font-medium tabular-nums text-gray-900">
              {Math.round(value).toLocaleString()}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function RoutewiseDecisionsPanel({ modelId }: RoutewiseDecisionsPanelProps) {
  const [range, setRange] = useState<RoutewiseDecisionsRange>('24h');
  const [decisions, setDecisions] = useState<RoutewiseDecisionsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!modelId) return;
    setLoading(true);
    setError(null);
    try {
      const decisionsResp = await getRoutewiseDecisions(modelId, range);
      setDecisions(decisionsResp);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [modelId, range]);

  useEffect(() => {
    void load();
  }, [load]);

  const distributionEndpoints = useMemo(() => {
    const fromShare = (decisions?.selection_share ?? []).map((item) => item.endpoint);
    if (fromShare.length > 0) return fromShare;
    const set = new Set<string>();
    for (const bucket of decisions?.buckets ?? []) {
      for (const key of Object.keys(bucket.counts)) set.add(key);
    }
    return Array.from(set);
  }, [decisions]);

  // Endpoint colors are stable across renders and use a categorical palette so
  // providers in the same RouteWise tier remain visually distinct.
  const distributionSeries = useMemo<DistributionSeries[]>(() => {
    const tierByEndpoint = new Map<string, string>();
    for (const item of decisions?.selection_share ?? []) {
      tierByEndpoint.set(item.endpoint, item.provider_type);
    }
    const usedColors = new Set<string>();
    return distributionEndpoints.map((endpoint, index) => {
      const tier = tierByEndpoint.get(endpoint) ?? null;
      return {
        endpoint,
        dataKey: `s${index}`,
        short: shortEndpoint(modelId, endpoint),
        tier,
        color: distributionColorForEndpoint(endpoint, usedColors),
      };
    });
  }, [decisions, distributionEndpoints, modelId]);

  const filledBuckets = useMemo(() => {
    if (!decisions) return [];
    return fillBucketGaps(
      decisions.buckets ?? [],
      decisions.bucket_seconds,
      RANGE_WINDOW_SECONDS[range],
      Date.now(),
    );
  }, [decisions, range]);

  const barData = useMemo(
    () =>
      filledBuckets.map((bucket) => {
        const row: Record<string, number | string> = {
          label: fmtBucketLabel(bucket.bucket_start, range),
        };
        distributionEndpoints.forEach((endpoint, index) => {
          row[`s${index}`] = bucket.counts[endpoint] ?? 0;
        });
        return row;
      }),
    [filledBuckets, distributionEndpoints, range],
  );

  const hedgeBarData = useMemo(
    () =>
      filledBuckets.map((bucket) => ({
        label: fmtBucketLabel(bucket.bucket_start, range),
        not_hedged: bucket.hedge.not_hedged,
        hedged_primary_won: bucket.hedge.hedged_primary_won,
        hedged_backup_won: bucket.hedge.hedged_backup_won,
      })),
    [filledBuckets, range],
  );

  const summary = decisions ? shareSummary(decisions.selection_share) : '';
  // Zero-filled series are non-empty whenever a response exists, so the empty
  // states key off the raw server buckets instead.
  const serverBucketCount = decisions?.buckets?.length ?? 0;
  const hasDistribution = serverBucketCount > 0 && distributionEndpoints.length > 0;

  const hedge = decisions?.hedge_summary ?? null;
  const hedgeRateText = fmtRatePct(hedge?.hedge_rate);
  // Backup win rate is undefined when nothing hedged, so show a dash there.
  const backupWinText = hedge && hedge.hedged > 0 ? fmtRatePct(hedge.backup_win_rate) : '—';
  const hasHedgeChart = serverBucketCount > 0;

  return (
    <section className="rounded-lg border border-gray-200 bg-white p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-[14px] font-semibold text-gray-900">RouteWise decisions</h2>
        <div className="flex items-center gap-2">
          <div className="flex gap-1">
            {RANGES.map(({ key, label }) => (
              <button
                key={key}
                type="button"
                onClick={() => setRange(key)}
                className={`rounded-md px-3 py-1.5 text-[12px] font-medium transition ${
                  range === key
                    ? 'bg-gray-900 text-white'
                    : 'text-gray-500 hover:bg-gray-100 hover:text-gray-900'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
          <button
            type="button"
            disabled={!modelId || loading}
            onClick={() => void load()}
            className="h-8 rounded-md border border-gray-200 px-3 text-[12px] font-medium text-gray-700 disabled:opacity-50"
          >
            {loading ? 'Loading...' : 'Refresh'}
          </button>
        </div>
      </div>

      {error && (
        <div className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-[12px] text-red-700">{error}</div>
      )}

      <div className="mt-6">
        <h3 className="text-[13px] font-semibold text-gray-900">Selection distribution</h3>
        {!hasDistribution ? (
          <div className="mt-2 rounded-lg border border-dashed border-gray-200 py-8 text-center text-[12px] text-gray-400">
            No RouteWise decisions in this window.
          </div>
        ) : (
          <>
            <div className="mt-2 h-[210px] w-full">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={barData} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
                  <CartesianGrid stroke={GRID_STROKE} strokeDasharray="3 3" />
                  <XAxis
                    dataKey="label"
                    interval={xTickInterval(barData.length)}
                    tickLine={false}
                    tick={AXIS_TICK}
                  />
                  <YAxis allowDecimals={false} width={36} tickLine={false} tick={AXIS_TICK} />
                  <Tooltip
                    cursor={{ fill: 'rgba(0,0,0,0.04)' }}
                    content={<DistributionTooltip series={distributionSeries} />}
                  />
                  <Legend wrapperStyle={LEGEND_STYLE} iconSize={10} />
                  {distributionSeries.map((series) => (
                    <Bar
                      key={series.endpoint}
                      dataKey={series.dataKey}
                      stackId="selection"
                      name={series.short}
                      fill={series.color}
                      maxBarSize={28}
                      isAnimationActive={false}
                    />
                  ))}
                </BarChart>
              </ResponsiveContainer>
            </div>
            {summary && (
              <p className="mt-2 text-[12px] text-gray-700" data-testid="selection-share-summary">
                {summary}
              </p>
            )}
          </>
        )}
      </div>

      <div className="mt-6 border-t border-gray-100 pt-4">
        <h3 className="text-[13px] font-semibold text-gray-900">Hedging</h3>
        <div
          className="mt-2 flex flex-wrap items-center gap-2 text-[12px]"
          data-testid="hedge-kpis"
        >
          <span className="rounded bg-gray-100 px-2 py-0.5 text-gray-700">
            hedge rate {hedgeRateText}
          </span>
          <span className="rounded bg-gray-100 px-2 py-0.5 text-gray-700">
            backup win rate {backupWinText}
          </span>
        </div>
        {!hasHedgeChart ? (
          <div className="mt-2 rounded-lg border border-dashed border-gray-200 py-8 text-center text-[12px] text-gray-400">
            No hedging activity in this window.
          </div>
        ) : (
          <div className="mt-2 h-[210px] w-full">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={hedgeBarData} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
                <CartesianGrid stroke={GRID_STROKE} strokeDasharray="3 3" />
                <XAxis
                  dataKey="label"
                  interval={xTickInterval(hedgeBarData.length)}
                  tickLine={false}
                  tick={AXIS_TICK}
                />
                <YAxis allowDecimals={false} width={36} tickLine={false} tick={AXIS_TICK} />
                <Tooltip cursor={{ fill: 'rgba(0,0,0,0.04)' }} contentStyle={TOOLTIP_STYLE} />
                <Legend wrapperStyle={LEGEND_STYLE} iconSize={10} />
                {HEDGE_SEGMENTS.map((segment) => (
                  <Bar
                    key={segment.key}
                    dataKey={segment.key}
                    stackId="hedge"
                    name={segment.label}
                    fill={segment.color}
                    maxBarSize={28}
                    isAnimationActive={false}
                  />
                ))}
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>
    </section>
  );
}
