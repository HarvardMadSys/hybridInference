'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { getRoutewiseDecisions, listRecentRequests, listRoutewiseSettings } from '@/lib/api/admin';
import type {
  AdminRecentRequestItem,
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

// Recent-decisions window (days) fetched for the explainer per range.
const RANGE_DAYS: Record<RoutewiseDecisionsRange, number> = {
  '24h': 1,
  '7d': 7,
  '30d': 30,
};

// Reused from AnalyticsTab conventions.
const CHART_COLORS = [
  '#3b82f6',
  '#f59e0b',
  '#10b981',
  '#8b5cf6',
  '#ec4899',
  '#06b6d4',
  '#f97316',
  '#84cc16',
  '#e11d48',
  '#7c3aed',
];

const TOOLTIP_STYLE = {
  fontSize: 12,
  borderRadius: 8,
  border: '1px solid #e5e7eb',
  boxShadow: '0 1px 3px rgba(0,0,0,0.08)',
  background: '#fff',
  padding: '6px 8px',
} as const;

// Paper tier notation (binding decision #8). Order is the legend order.
const TIER_ORDER = ['on_demand', 'quota', 'concurrency'] as const;
const TIER_META: Record<string, { label: string; color: string }> = {
  on_demand: { label: 'on_demand (𝒫_O)', color: '#3b82f6' },
  quota: { label: 'quota (𝒫_Q)', color: '#f59e0b' },
  concurrency: { label: 'concurrency (𝒫_C)', color: '#10b981' },
};

const ALPHA_SETTING_KEY = 'routewise_budget_alpha';

// Hedge stack, bottom to top. not_hedged is the panel's muted neutral gray;
// hedged_primary_won a light orange; hedged_backup_won the paper hedge orange.
const HEDGE_SEGMENTS = [
  { key: 'not_hedged', label: 'not hedged', color: '#d1d5db' },
  { key: 'hedged_primary_won', label: 'hedged · primary won', color: '#f8c471' },
  { key: 'hedged_backup_won', label: 'hedged · backup won', color: '#f28e2b' },
] as const;

function shortEndpoint(modelId: string, endpoint: string): string {
  let label = endpoint;
  const prefix = `${modelId}:`;
  if (label.startsWith(prefix)) label = label.slice(prefix.length);
  return label.replace(/-api$/, '');
}

// Even shorter label for the one-line share summary: the provider slug inside
// the bracket when present, else the stripped endpoint.
function shareLabel(modelId: string, endpoint: string): string {
  const match = /\[([^\]]+)\]/.exec(endpoint);
  if (match) return match[1];
  return shortEndpoint(modelId, endpoint);
}

function tierLabel(type: string): string {
  return TIER_META[type]?.label ?? type;
}

function tierColor(type: string): string {
  return TIER_META[type]?.color ?? '#9ca3af';
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

function fmtTimestamp(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function fmtUsd(value: number | null | undefined, digits = 4): string {
  if (value == null || !Number.isFinite(value)) return '—';
  return `$${value.toFixed(digits)}`;
}

function fmtCost(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—';
  if (value === 0) return '$0';
  return `$${value.toFixed(6).replace(/0+$/, '').replace(/\.$/, '')}`;
}

function fmtTtft(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—';
  return `${value.toFixed(3)} s`;
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

function shareSummary(
  share: RoutewiseDecisionsResponse['selection_share'],
  modelId: string,
): string {
  const total = share.reduce((sum, item) => sum + item.count, 0);
  if (!total) return '';
  return share
    .map((item) => `${shareLabel(modelId, item.endpoint)} ${fmtPct(item.count, total)}`)
    .join(' · ');
}

function hasCandidateBlob(row: AdminRecentRequestItem): boolean {
  const costs = row.routewise?.candidate_costs_usd;
  return Boolean(costs && Object.keys(costs).length > 0);
}

type CandidatePoint = {
  endpoint: string;
  short: string;
  cost: number;
  ttft: number;
  tier: string;
  weight: number;
  selected: boolean;
  source: string | null;
  quotaRemaining: number | null;
};

// Custom marker: selected (LP weight > 0) candidates render solid and larger;
// others translucent.
function CandidateDot(props: {
  cx?: number;
  cy?: number;
  fill?: string;
  payload?: CandidatePoint;
}) {
  const { cx, cy, fill, payload } = props;
  if (cx == null || cy == null || Number.isNaN(cx) || Number.isNaN(cy)) return null;
  const selected = Boolean(payload?.selected);
  return (
    <circle
      cx={cx}
      cy={cy}
      r={selected ? 6 : 4}
      fill={fill}
      fillOpacity={selected ? 1 : 0.35}
      stroke={selected ? '#111827' : 'none'}
      strokeWidth={selected ? 1 : 0}
    />
  );
}

function CandidateTooltip(props: { active?: boolean; payload?: { payload: CandidatePoint }[] }) {
  const { active, payload } = props;
  if (!active || !payload || payload.length === 0) return null;
  const point = payload[0].payload;
  return (
    <div style={TOOLTIP_STYLE}>
      <div className="font-medium text-gray-900">{point.short}</div>
      <div className="text-gray-600">cost {fmtCost(point.cost)}</div>
      <div className="text-gray-600">TTFT {fmtTtft(point.ttft)}</div>
      <div className="text-gray-600">tier {tierLabel(point.tier)}</div>
      <div className="text-gray-600">lp weight {point.weight.toFixed(2)}</div>
      {point.source && <div className="text-gray-600">ttft source {point.source}</div>}
      {point.quotaRemaining != null && (
        <div className="text-gray-600">quota remaining {point.quotaRemaining.toLocaleString()}</div>
      )}
    </div>
  );
}

export function RoutewiseDecisionsPanel({ modelId }: RoutewiseDecisionsPanelProps) {
  const [range, setRange] = useState<RoutewiseDecisionsRange>('24h');
  const [decisions, setDecisions] = useState<RoutewiseDecisionsResponse | null>(null);
  const [decisionRows, setDecisionRows] = useState<AdminRecentRequestItem[]>([]);
  const [selectedRequestId, setSelectedRequestId] = useState<string | null>(null);
  const [alpha, setAlpha] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!modelId) return;
    setLoading(true);
    setError(null);
    try {
      const [decisionsResp, recentResp] = await Promise.all([
        getRoutewiseDecisions(modelId, range),
        listRecentRequests(20, 0, undefined, modelId, false, 'chat', RANGE_DAYS[range]),
      ]);
      setDecisions(decisionsResp);
      const rows = (recentResp.requests ?? []).filter(hasCandidateBlob);
      setDecisionRows(rows);
      setSelectedRequestId((current) => {
        if (current && rows.some((row) => row.request_id === current)) return current;
        return rows[0]?.request_id ?? null;
      });
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
    // Alpha is read-only and best-effort: a missing setting must not break the
    // panel, so it loads separately and swallows its own errors.
    try {
      const settings = await listRoutewiseSettings();
      const found = settings.settings.find((item) => item.key === ALPHA_SETTING_KEY);
      const value = typeof found?.value === 'number' ? found.value : Number(found?.value);
      setAlpha(found && Number.isFinite(value) ? value : null);
    } catch {
      setAlpha(null);
    }
  }, [modelId, range]);

  useEffect(() => {
    void load();
  }, [load]);

  const selectedDecision = useMemo(
    () => decisionRows.find((row) => row.request_id === selectedRequestId) ?? null,
    [decisionRows, selectedRequestId],
  );

  const distributionEndpoints = useMemo(() => {
    const fromShare = (decisions?.selection_share ?? []).map((item) => item.endpoint);
    if (fromShare.length > 0) return fromShare;
    const set = new Set<string>();
    for (const bucket of decisions?.buckets ?? []) {
      for (const key of Object.keys(bucket.counts)) set.add(key);
    }
    return Array.from(set);
  }, [decisions]);

  const barData = useMemo(
    () =>
      (decisions?.buckets ?? []).map((bucket) => {
        const row: Record<string, number | string> = {
          label: fmtBucketLabel(bucket.bucket_start, range),
        };
        distributionEndpoints.forEach((endpoint, index) => {
          row[`s${index}`] = bucket.counts[endpoint] ?? 0;
        });
        return row;
      }),
    [decisions, distributionEndpoints, range],
  );

  const hedgeBarData = useMemo(
    () =>
      (decisions?.buckets ?? []).map((bucket) => ({
        label: fmtBucketLabel(bucket.bucket_start, range),
        not_hedged: bucket.hedge.not_hedged,
        hedged_primary_won: bucket.hedge.hedged_primary_won,
        hedged_backup_won: bucket.hedge.hedged_backup_won,
      })),
    [decisions, range],
  );

  const candidatePoints = useMemo<CandidatePoint[]>(() => {
    const rw = selectedDecision?.routewise;
    if (!rw?.candidate_costs_usd) return [];
    const costs = rw.candidate_costs_usd;
    const ttfts = rw.candidate_mean_ttft_sec ?? {};
    const types = rw.candidate_provider_types ?? {};
    const weights = rw.lp_weights ?? {};
    const sources = rw.candidate_mean_ttft_sources ?? {};
    const quota = rw.candidate_quota_remaining ?? {};
    return Object.keys(costs)
      .map((endpoint) => {
        const cost = costs[endpoint];
        const ttft = ttfts[endpoint];
        const weight = weights[endpoint] ?? 0;
        return {
          endpoint,
          short: shortEndpoint(modelId, endpoint),
          cost,
          ttft,
          tier: types[endpoint] ?? 'unknown',
          weight,
          selected: weight > 0,
          source: sources[endpoint] ?? null,
          quotaRemaining: quota[endpoint] ?? null,
        };
      })
      .filter((point) => Number.isFinite(point.cost) && Number.isFinite(point.ttft));
  }, [selectedDecision, modelId]);

  const candidatesByTier = useMemo(() => {
    const grouped = new Map<string, CandidatePoint[]>();
    for (const point of candidatePoints) {
      const list = grouped.get(point.tier) ?? [];
      list.push(point);
      grouped.set(point.tier, list);
    }
    return grouped;
  }, [candidatePoints]);

  const tierKeys = useMemo(() => {
    const known = TIER_ORDER.filter((tier) => candidatesByTier.has(tier));
    const extra = Array.from(candidatesByTier.keys()).filter(
      (tier) => !(TIER_ORDER as readonly string[]).includes(tier),
    );
    return [...known, ...extra];
  }, [candidatesByTier]);

  const budget = selectedDecision?.routewise?.budget_usd ?? null;
  const lpStatus = selectedDecision?.routewise?.lp_status ?? null;
  const hedged = selectedDecision?.routewise?.hedged === true;
  const hedgeWinner = selectedDecision?.routewise?.hedge_winner ?? null;
  const backupProvider = selectedDecision?.routewise?.backup_provider ?? null;
  const fallbackAttempts = selectedDecision?.routewise?.fallback_attempts ?? 0;

  const infoParts: string[] = [];
  if (alpha != null) infoParts.push(`α = ${alpha}`);
  if (budget != null) infoParts.push(`budget ${fmtUsd(budget)}`);
  if (lpStatus) infoParts.push(lpStatus);

  const summary = decisions ? shareSummary(decisions.selection_share, modelId) : '';
  const unattributed = decisions?.unattributed_requests ?? 0;
  const hasDistribution = barData.length > 0 && distributionEndpoints.length > 0;

  const hedge = decisions?.hedge_summary ?? null;
  const hedgeRateText = fmtRatePct(hedge?.hedge_rate);
  // Backup win rate is undefined when nothing hedged, so show a dash there.
  const backupWinText = hedge && hedge.hedged > 0 ? fmtRatePct(hedge.backup_win_rate) : '—';
  const medianDelay = hedge?.median_hedge_delay_ms ?? null;
  const medianDelayText = medianDelay != null ? `${Math.round(medianDelay)} ms` : '—';
  const hasHedgeChart = hedgeBarData.length > 0;

  return (
    <section className="rounded-lg border border-gray-200 bg-white p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-[14px] font-semibold text-gray-900">RouteWise decisions</h2>
          <p className="mt-1 text-[12px] text-gray-500">
            How RouteWise routed {modelId || 'this model'}: which provider legs it used, in what
            share, and why.
          </p>
        </div>
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

      <div className="mt-4">
        <h3 className="text-[13px] font-semibold text-gray-900">Selection distribution</h3>
        {!hasDistribution ? (
          <div className="mt-2 rounded-lg border border-dashed border-gray-200 py-8 text-center text-[12px] text-gray-400">
            No RouteWise decisions in this window.
          </div>
        ) : (
          <>
            <div className="mt-2 h-[240px] w-full">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={barData} margin={{ top: 8, right: 12, bottom: 8, left: 0 }}>
                  <CartesianGrid stroke="#f1f5f9" strokeDasharray="3 3" />
                  <XAxis dataKey="label" tick={{ fontSize: 10, fill: '#6b7280' }} />
                  <YAxis
                    allowDecimals={false}
                    width={36}
                    tick={{ fontSize: 10, fill: '#6b7280' }}
                  />
                  <Tooltip contentStyle={TOOLTIP_STYLE} />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  {distributionEndpoints.map((endpoint, index) => (
                    <Bar
                      key={endpoint}
                      dataKey={`s${index}`}
                      stackId="selection"
                      name={shortEndpoint(modelId, endpoint)}
                      fill={CHART_COLORS[index % CHART_COLORS.length]}
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
            {unattributed > 0 && (
              <p className="mt-1 text-[11px] text-gray-400">{unattributed} unattributed</p>
            )}
            <p className="mt-1 text-[11px] text-gray-400">
              Hedge backup wins are attributed to the backup endpoint.
            </p>
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
          <span className="rounded bg-gray-100 px-2 py-0.5 text-gray-700">
            median hedge delay {medianDelayText}
          </span>
        </div>
        {!hasHedgeChart ? (
          <div className="mt-2 rounded-lg border border-dashed border-gray-200 py-8 text-center text-[12px] text-gray-400">
            No hedging activity in this window.
          </div>
        ) : (
          <div className="mt-2 h-[240px] w-full">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={hedgeBarData} margin={{ top: 8, right: 12, bottom: 8, left: 0 }}>
                <CartesianGrid stroke="#f1f5f9" strokeDasharray="3 3" />
                <XAxis dataKey="label" tick={{ fontSize: 10, fill: '#6b7280' }} />
                <YAxis allowDecimals={false} width={36} tick={{ fontSize: 10, fill: '#6b7280' }} />
                <Tooltip contentStyle={TOOLTIP_STYLE} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                {HEDGE_SEGMENTS.map((segment) => (
                  <Bar
                    key={segment.key}
                    dataKey={segment.key}
                    stackId="hedge"
                    name={segment.label}
                    fill={segment.color}
                    isAnimationActive={false}
                  />
                ))}
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>

      <div className="mt-6 border-t border-gray-100 pt-4">
        <h3 className="text-[13px] font-semibold text-gray-900">Decision explainer</h3>
        <p className="mt-1 text-[12px] text-gray-500">
          Pick a recent request to see its candidate cost × TTFT trade-off.
        </p>

        {decisionRows.length === 0 ? (
          <div className="mt-2 rounded-lg border border-dashed border-gray-200 py-8 text-center text-[12px] text-gray-400">
            No per-request decisions with candidate data in this window.
          </div>
        ) : (
          <div className="mt-3 grid gap-4 lg:grid-cols-[minmax(0,260px)_minmax(0,1fr)]">
            <div className="max-h-[300px] overflow-y-auto rounded-lg border border-gray-100">
              <div className="divide-y divide-gray-100">
                {decisionRows.map((row) => {
                  const selectedEndpoint =
                    row.routewise?.final_endpoint ?? row.routewise?.selected_endpoint ?? null;
                  const status = row.routewise?.lp_status ?? null;
                  const isActive = row.request_id === selectedRequestId;
                  return (
                    <button
                      key={row.request_id}
                      type="button"
                      onClick={() => setSelectedRequestId(row.request_id)}
                      className={`block w-full px-3 py-2 text-left text-[12px] ${
                        isActive ? 'bg-gray-50' : 'bg-white hover:bg-gray-50'
                      }`}
                    >
                      <div className="text-gray-500">{fmtTimestamp(row.timestamp)}</div>
                      <div className="truncate font-medium text-gray-800">
                        {selectedEndpoint ? shortEndpoint(modelId, selectedEndpoint) : '—'}
                      </div>
                      {status && <div className="text-[11px] text-gray-400">{status}</div>}
                    </button>
                  );
                })}
              </div>
            </div>

            <div className="min-w-0">
              {infoParts.length > 0 && (
                <div className="flex flex-wrap items-center gap-2 text-[12px] text-gray-600">
                  <span data-testid="decision-info-line">{infoParts.join(' · ')}</span>
                  {hedged && (
                    <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[11px] font-medium text-amber-700">
                      hedged
                      {hedgeWinner ? ` · ${hedgeWinner}` : ''}
                      {backupProvider ? ` → ${shortEndpoint(modelId, backupProvider)}` : ''}
                    </span>
                  )}
                  {fallbackAttempts > 0 && (
                    <span className="rounded bg-red-50 px-1.5 py-0.5 text-[11px] font-medium text-red-700">
                      fallback ×{fallbackAttempts}
                    </span>
                  )}
                </div>
              )}

              {candidatePoints.length === 0 ? (
                <div className="mt-2 rounded-lg border border-dashed border-gray-200 py-8 text-center text-[12px] text-gray-400">
                  No candidate data for this decision.
                </div>
              ) : (
                <div className="mt-2 h-[280px] w-full">
                  <ResponsiveContainer width="100%" height="100%">
                    <ScatterChart margin={{ top: 8, right: 16, bottom: 24, left: 8 }}>
                      <CartesianGrid stroke="#f1f5f9" strokeDasharray="3 3" />
                      <XAxis
                        type="number"
                        dataKey="cost"
                        name="cost"
                        tick={{ fontSize: 10, fill: '#6b7280' }}
                        tickFormatter={(value: number) => fmtCost(value)}
                        label={{
                          value: 'cost (USD)',
                          position: 'insideBottom',
                          offset: -12,
                          style: { fontSize: 11, fill: '#6b7280' },
                        }}
                      />
                      <YAxis
                        type="number"
                        dataKey="ttft"
                        name="TTFT"
                        width={44}
                        tick={{ fontSize: 10, fill: '#6b7280' }}
                        label={{
                          value: 'TTFT (s)',
                          angle: -90,
                          position: 'insideLeft',
                          style: { fontSize: 11, fill: '#6b7280', textAnchor: 'middle' },
                        }}
                      />
                      <Tooltip cursor={{ strokeDasharray: '3 3' }} content={<CandidateTooltip />} />
                      <Legend wrapperStyle={{ fontSize: 11 }} />
                      {budget != null && (
                        <ReferenceLine
                          x={budget}
                          stroke="#111827"
                          strokeDasharray="4 3"
                          label={{
                            value: 'budget',
                            position: 'top',
                            fontSize: 11,
                            fill: '#111827',
                          }}
                        />
                      )}
                      {tierKeys.map((tier) => (
                        <Scatter
                          key={tier}
                          name={tierLabel(tier)}
                          data={candidatesByTier.get(tier)}
                          fill={tierColor(tier)}
                          shape={<CandidateDot />}
                          isAnimationActive={false}
                        />
                      ))}
                    </ScatterChart>
                  </ResponsiveContainer>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </section>
  );
}
