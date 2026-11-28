'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { useAuth } from '@/components/providers';
import {
  getOpenInfraDashboard,
  getOpenInfraHistory,
  type FleetHealth,
  type OpenInfraDashboard,
  type OpenInfraHistory,
} from '@/lib/api/openinfra';
import { fill } from '@/lib/utils/interpolate';
import { buildCharts, type ChartSpec } from './charts';
import { ExportControl } from './ExportControl';
import { NONE, useInfraFormat, type InfraFormat } from './format';
import { smallButton } from './InfraDialog';
import {
  DEFAULT_HOURS,
  RANGES,
  deployFromHash,
  deployKey,
  filterDash,
  filterHist,
  findDeploy,
  historyRows,
  hoursFromSearch,
  kpisFromHist,
  perMin,
  prefillDecodeTps,
  preferLive,
  resolveDeploy,
  viewFleet,
  type RangeKpis,
} from './model';
import {
  EngineCards,
  GpuFleet,
  KpiTiles,
  RecentRequests,
  SectionHeading,
  ServedSection,
  type Tile,
} from './sections';
import { ChartPanel } from './TimeSeriesChart';

/** How often the page asks for fresh numbers while it is visible. */
export const REFRESH_MS = 30_000;

type Phase = 'loading' | 'ready' | 'unavailable' | 'error';

const PILL: Record<FleetHealth, string> = {
  healthy: 'border-emerald-200 bg-emerald-50 text-emerald-700',
  busy: 'border-amber-200 bg-amber-50 text-amber-800',
  degraded: 'border-red-200 bg-red-50 text-red-700',
  offline: 'border-gray-200 bg-gray-100 text-gray-600',
};

function fleetLabel(fleet: FleetHealth, f: InfraFormat): string {
  const { t } = f;
  switch (fleet) {
    case 'healthy':
      return t('openinfra.fleet.healthy', 'healthy');
    case 'busy':
      return t('openinfra.fleet.busy', 'busy');
    case 'degraded':
      return t('openinfra.fleet.degraded', 'degraded');
    default:
      return t('openinfra.fleet.offline', 'offline');
  }
}

function rangeButtonLabel(hours: number, f: InfraFormat): { label: string; title: string } {
  const { t } = f;
  if (hours === 2) {
    return {
      label: t('openinfra.range.2h', '2h'),
      title: t('openinfra.range.2h_title', '1-minute samples'),
    };
  }
  if (hours === 24) {
    return {
      label: t('openinfra.range.24h', '24h'),
      title: t('openinfra.range.24h_title', '20-minute samples'),
    };
  }
  return {
    label: t('openinfra.range.7d', '7d'),
    title: t('openinfra.range.7d_title', 'hourly samples'),
  };
}

/** What a tile's figure is: an average over the range shown, or the live snapshot. */
function rangeNote(k: RangeKpis, hours: number, f: InfraFormat): string {
  const { t } = f;
  if (!k.averaged) return t('openinfra.range.live', 'live');
  if (hours > 48) {
    return fill(t('openinfra.range.avg_days', '{days}d avg'), { days: f.n(hours / 24, 1) });
  }
  return fill(t('openinfra.range.avg_hours', '{hours}h avg'), { hours: f.n(hours, 2) });
}

function UpdatedAgo({ ts, f }: { ts: number; f: InfraFormat }) {
  const { t } = f;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  return (
    <span className="text-sm text-gray-500">
      {fill(t('openinfra.updated', 'Updated {ago}'), { ago: f.ago(ts, now) })}
    </span>
  );
}

function ChartGrid({
  charts,
  deploy,
  f,
  columns,
}: {
  charts: ChartSpec[];
  deploy: string;
  f: InfraFormat;
  columns: 'two' | 'three';
}) {
  return (
    <div className={`grid gap-3 md:grid-cols-2 ${columns === 'three' ? 'xl:grid-cols-3' : ''}`}>
      {charts.map((spec) => (
        <ChartPanel key={spec.id} spec={spec} deploy={deploy} f={f} />
      ))}
    </div>
  );
}

function tiles(k: RangeKpis, hours: number, f: InfraFormat) {
  const { t } = f;
  const range = rangeNote(k, hours, f);
  const memPct = k.memory_total_mb ? (100 * (k.memory_used_mb || 0)) / k.memory_total_mb : null;
  const ramPct = k.ram_total_mb ? (100 * (k.ram_used_mb || 0)) / k.ram_total_mb : null;
  const total = prefillDecodeTps(k);
  const host: Tile[] = [
    {
      key: 'cpu',
      label: t('openinfra.kpi.cpu', 'CPU'),
      value: f.pct(k.cpu_util_pct),
      hint: fill(t('openinfra.kpi.cpu_hint', '{range} · load {load}'), {
        range,
        load: f.n(k.load1, 2),
      }),
    },
    {
      key: 'ram',
      label: t('openinfra.kpi.ram', 'RAM'),
      value: f.pct(ramPct),
      hint: fill(t('openinfra.kpi.memory_hint', '{range} · {used} / {total} GB'), {
        range,
        used: f.gb(k.ram_used_mb),
        total: f.gb(k.ram_total_mb),
      }),
    },
  ];
  const gpu: Tile[] = [
    {
      key: 'sm',
      label: t('openinfra.kpi.sm', 'SM util'),
      value: f.pct(k.avg_utilization_pct),
      hint: fill(t('openinfra.kpi.sm_hint', '{range} · tensor {tensor}'), {
        range,
        tensor: f.pct((k.tensor_active || 0) * 100),
      }),
    },
    {
      key: 'vram',
      label: t('openinfra.kpi.vram', 'VRAM'),
      value: f.pct(memPct),
      hint: fill(t('openinfra.kpi.memory_hint', '{range} · {used} / {total} GB'), {
        range,
        used: f.gb(k.memory_used_mb, 1),
        total: f.gb(k.memory_total_mb, 1),
      }),
    },
  ];
  const engine: Tile[] = [
    {
      key: 'queue',
      label: t('openinfra.kpi.queue', 'Queue'),
      value: f.n(k.queue_reqs, 1),
      hint: fill(t('openinfra.kpi.queue_hint', '{range} · {running} running'), {
        range,
        running: f.n(k.running_reqs, 1),
      }),
    },
    {
      key: 'tokens',
      label: t('openinfra.kpi.tokens', 'Prefill + decode'),
      value: total === null ? NONE : f.compact(perMin(total)),
      hint: fill(t('openinfra.kpi.tokens_hint', '{range} · tokens / min'), { range }),
    },
    {
      key: 'cache',
      label: t('openinfra.kpi.cache_hit', 'Cache hit'),
      value:
        k.cache_hit_rate === null || k.cache_hit_rate === undefined
          ? NONE
          : f.pct(k.cache_hit_rate * 100),
      hint: fill(t('openinfra.kpi.cache_hit_hint', '{range} · prefix cache'), { range }),
    },
    {
      key: 'kv',
      label: t('openinfra.kpi.kv_fill', 'KV fill'),
      value: f.pct((k.token_usage || 0) * 100),
      hint:
        k.used_tokens !== null && k.used_tokens !== undefined
          ? fill(t('openinfra.kpi.kv_fill_hint', '{range} · {tokens} tokens'), {
              range,
              tokens: f.compact(k.used_tokens),
            })
          : range,
    },
  ];
  return { host, gpu, engine };
}

/**
 * The public OpenInfra page: live metrics of the deployment's own servers,
 * one deployment at a time (named after `#` in the address) over the range in
 * `?hours=`, refreshed every 30 seconds while the page is visible.
 */
export function OpenInfraContent(): JSX.Element {
  const f = useInfraFormat();
  const { t } = f;
  const { state } = useAuth();
  const isAdmin = Boolean(state.user?.is_admin);

  // Read from the address once mounted, so the server and the first client
  // render agree.
  const [hours, setHours] = useState<number | null>(null);
  const [selected, setSelected] = useState('');
  const [dash, setDash] = useState<OpenInfraDashboard | null>(null);
  const [hist, setHist] = useState<OpenInfraHistory | null>(null);
  const [phase, setPhase] = useState<Phase>('loading');
  const [problem, setProblem] = useState<'history' | 'refresh' | null>(null);
  const [loading, setLoading] = useState(false);
  const loadSeq = useRef(0);
  const haveDash = useRef(false);
  const loadedAt = useRef(0);

  useEffect(() => {
    const fromAddress = () => {
      setHours(hoursFromSearch(window.location.search));
      setSelected(deployFromHash(window.location.hash));
    };
    fromAddress();
    const onHash = () => setSelected(deployFromHash(window.location.hash));
    window.addEventListener('hashchange', onHash);
    window.addEventListener('popstate', fromAddress);
    return () => {
      window.removeEventListener('hashchange', onHash);
      window.removeEventListener('popstate', fromAddress);
    };
  }, []);

  // Only the newest load's answer is used: a range picked meanwhile, or the
  // page closing, makes an older one moot.
  const load = useCallback(async (h: number) => {
    const id = ++loadSeq.current;
    setLoading(true);
    const [dashboard, history] = await Promise.all([
      getOpenInfraDashboard(),
      getOpenInfraHistory(h),
    ]);
    if (id !== loadSeq.current) return;
    setLoading(false);
    loadedAt.current = Date.now();
    if (dashboard.status === 'unavailable') {
      setPhase('unavailable');
      return;
    }
    if (dashboard.status === 'error') {
      // Keep what is on screen and say it is stale, rather than blank it.
      if (haveDash.current) setProblem('refresh');
      else setPhase('error');
      return;
    }
    haveDash.current = true;
    setDash(dashboard.data);
    setPhase('ready');
    if (history.status === 'ok') {
      setHist(history.data);
      setProblem(null);
    } else {
      setProblem('history');
    }
  }, []);

  useEffect(
    () => () => {
      loadSeq.current += 1;
    },
    [],
  );

  useEffect(() => {
    if (hours === null) return;
    void load(hours);
    const timer = setInterval(() => {
      if (document.visibilityState !== 'hidden') void load(hours);
    }, REFRESH_MS);
    const onVisible = () => {
      if (document.visibilityState === 'visible' && Date.now() - loadedAt.current >= REFRESH_MS) {
        void load(hours);
      }
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [hours, load]);

  const deploy = dash ? resolveDeploy(dash, selected) : '';

  // An address naming no deployment, or one that is gone, is corrected to the
  // one shown, so it can be shared as is.
  useEffect(() => {
    if (!dash || !deploy) return;
    const wanted = `#${encodeURIComponent(deploy)}`;
    const { hash, pathname, search } = window.location;
    if (
      hash !== wanted &&
      (hash === '' || hash === '#fleet' || !findDeploy(dash, deployFromHash(hash)))
    ) {
      window.history.replaceState(null, '', `${pathname}${search}${wanted}`);
    }
  }, [dash, deploy]);

  const chooseDeploy = (id: string) => {
    const { pathname, search } = window.location;
    const next = `#${encodeURIComponent(id)}`;
    if (window.location.hash !== next) {
      window.history.pushState(null, '', `${pathname}${search}${next}`);
    }
    setSelected(id);
  };

  const chooseHours = (h: number) => {
    const url = new URL(window.location.href);
    if (h === DEFAULT_HOURS) url.searchParams.delete('hours');
    else url.searchParams.set('hours', String(h));
    const next = `${url.pathname}${url.search}${url.hash}`;
    if (next !== `${window.location.pathname}${window.location.search}${window.location.hash}`) {
      window.history.pushState(null, '', next);
    }
    if (h === hours) void load(h);
    else setHours(h);
  };

  const view = useMemo(() => (dash ? filterDash(dash, deploy) : null), [dash, deploy]);
  const histView = useMemo(() => filterHist(historyRows(hist), dash, deploy), [hist, dash, deploy]);
  const charts = useMemo(() => buildCharts(histView, f), [histView, f]);
  const kpis = useMemo(() => (view ? kpisFromHist(view, histView) : null), [view, histView]);
  const shown = dash ? findDeploy(dash, deploy) : null;
  const tabs = dash ? preferLive(dash.sglang) : [];
  const fleet = view ? viewFleet(view) : null;
  const kpiTiles = kpis && hours !== null ? tiles(kpis, hours, f) : null;

  return (
    <div className="space-y-8">
      <header className="space-y-4">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="space-y-1">
            <h1 className="text-3xl font-bold tracking-tight text-gray-950">
              {t('openinfra.title', 'OpenInfra')}
            </h1>
            <p className="max-w-3xl text-gray-600">
              {t(
                'openinfra.subtitle',
                'Live GPU, host and inference-engine metrics from this deployment’s own servers.',
              )}
            </p>
            {shown ? (
              <p className="text-sm text-gray-500">
                {fill(t('openinfra.deploy_line', '{name} · GPU {gpus} · {host}'), {
                  name: deployKey(shown),
                  gpus: shown.gpu_indexes || NONE,
                  host: shown.hostname || shown.node_id || NONE,
                })}
              </p>
            ) : null}
          </div>
          {phase === 'ready' && dash ? (
            <div className="flex flex-wrap items-center gap-2">
              {fleet ? (
                <span
                  className={`rounded-full border px-2.5 py-0.5 text-xs font-semibold ${PILL[fleet]}`}
                >
                  {fleetLabel(fleet, f)}
                </span>
              ) : null}
              <UpdatedAgo ts={dash.generated_at} f={f} />
              <button
                type="button"
                className={smallButton}
                disabled={loading}
                onClick={() => {
                  if (hours !== null) void load(hours);
                }}
              >
                {loading
                  ? t('openinfra.refreshing', 'Refreshing…')
                  : t('openinfra.refresh', 'Refresh')}
              </button>
              {isAdmin ? <ExportControl f={f} /> : null}
            </div>
          ) : null}
        </div>
        {phase === 'ready' ? (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <nav
              aria-label={t('openinfra.tabs.deployments', 'Deployments')}
              className="flex flex-wrap gap-1.5"
            >
              {tabs.map((server) => {
                const id = deployKey(server);
                const on = shown ? deployKey(shown) === id : false;
                return (
                  <button
                    key={server.id}
                    type="button"
                    aria-pressed={on}
                    onClick={() => chooseDeploy(id)}
                    className={`rounded-full border px-3 py-1 text-sm font-medium ${
                      on
                        ? 'border-gray-900 bg-gray-900 text-white'
                        : 'border-gray-200 bg-white text-gray-700 hover:bg-gray-50'
                    }`}
                  >
                    {id}
                  </button>
                );
              })}
            </nav>
            <div
              role="group"
              aria-label={t('openinfra.tabs.range', 'Time range')}
              className="flex gap-1"
            >
              {RANGES.map((range) => {
                const { label, title } = rangeButtonLabel(range.hours, f);
                const on = range.hours === hours;
                return (
                  <button
                    key={range.hours}
                    type="button"
                    title={title}
                    aria-pressed={on}
                    onClick={() => chooseHours(range.hours)}
                    className={`rounded-md border px-2.5 py-1 text-sm font-medium tabular-nums ${
                      on
                        ? 'border-gray-900 bg-gray-900 text-white'
                        : 'border-gray-200 bg-white text-gray-700 hover:bg-gray-50'
                    }`}
                  >
                    {label}
                  </button>
                );
              })}
            </div>
          </div>
        ) : null}
      </header>

      {phase === 'loading' ? (
        <p className="text-sm text-gray-500">{t('openinfra.loading', 'Loading OpenInfra…')}</p>
      ) : null}
      {phase === 'unavailable' ? (
        <p className="text-sm text-gray-600">
          {t('openinfra.unavailable', 'OpenInfra is not published for this site.')}
        </p>
      ) : null}
      {phase === 'error' ? (
        <p className="text-sm text-gray-600">
          {t('openinfra.error', 'OpenInfra could not be loaded. Try again in a few minutes.')}
        </p>
      ) : null}
      {phase === 'ready' && problem ? (
        <p
          role="status"
          className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800"
        >
          {problem === 'history'
            ? t(
                'openinfra.history_error',
                'The charts could not be refreshed; they show the last history loaded.',
              )
            : t(
                'openinfra.refresh_error',
                'The latest numbers could not be loaded; the page shows the last ones.',
              )}
        </p>
      ) : null}

      {phase === 'ready' && view && kpiTiles ? (
        <>
          <ServedSection view={view} f={f} />

          <section className="space-y-3" aria-labelledby="openinfra-host">
            <SectionHeading
              id="openinfra-host"
              title={t('openinfra.host.title', 'Host')}
              subtitle={t('openinfra.host.subtitle', 'CPU and system memory')}
            />
            <KpiTiles tiles={kpiTiles.host} label={t('openinfra.host.kpis', 'Host figures')} />
            <ChartGrid charts={charts.host} deploy={deploy} f={f} columns="two" />
          </section>

          <section className="space-y-3" aria-labelledby="openinfra-gpu">
            <SectionHeading
              id="openinfra-gpu"
              title={t('openinfra.gpu.title', 'GPU')}
              subtitle={t('openinfra.gpu.subtitle', 'SM, memory, power and interconnect')}
            />
            <KpiTiles tiles={kpiTiles.gpu} label={t('openinfra.gpu.kpis', 'GPU figures')} />
            <ChartGrid charts={charts.gpu} deploy={deploy} f={f} columns="three" />
            <GpuFleet view={view} f={f} />
          </section>

          <section className="space-y-3" aria-labelledby="openinfra-engine">
            <SectionHeading
              id="openinfra-engine"
              title={t('openinfra.engine.title', 'Engine')}
              subtitle={t('openinfra.engine.subtitle', 'SGLang and vLLM serving')}
            />
            <KpiTiles
              tiles={kpiTiles.engine}
              label={t('openinfra.engine.kpis', 'Engine figures')}
            />
            <ChartGrid charts={charts.engine} deploy={deploy} f={f} columns="three" />
            <EngineCards view={view} f={f} />
            <RecentRequests view={view} f={f} />
          </section>

          <p className="text-xs text-gray-500">
            {t(
              'openinfra.footer',
              'Refreshed every 30 seconds · hover a chart for values · Ctrl or ⌘ + scroll to zoom · drag to pan · double-click to reset · Zoom opens a chart larger',
            )}
          </p>
        </>
      ) : null}
    </div>
  );
}
