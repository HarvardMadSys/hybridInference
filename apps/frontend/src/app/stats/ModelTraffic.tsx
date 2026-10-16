'use client';

import { useState, type KeyboardEvent, type PointerEvent } from 'react';
import type { ModelTraffic, PublicStats } from '@/lib/api/publicStats';
import { KIND_COLORS, fmtCompact, fmtDay, fmtPercent } from './format';

type Metric = 'tokens' | 'requests';

const BAR = KIND_COLORS.coding;
const BAR_ACTIVE = '#1b4f8f';
const OTHER_LABEL = 'Other models';

function daily(series: ModelTraffic, metric: Metric): number[] {
  return metric === 'tokens' ? series.daily_tokens : series.daily_requests;
}

/**
 * Bars for one model's days, scaled to its own busiest day. Hover or focus a
 * day for its value; arrow keys move between days.
 */
function DailyBars({
  label,
  dates,
  values,
  metric,
}: {
  label: string;
  dates: string[];
  values: number[];
  metric: Metric;
}) {
  const [active, setActive] = useState<number | null>(null);
  const n = values.length;
  const max = Math.max(...values, 1);
  const peak = values.indexOf(Math.max(...values));
  // Leave air between bars only while there are few enough to see it.
  const gap = n <= 60 ? 0.25 : 0;
  const unit = metric === 'tokens' ? 'tokens' : 'requests';

  const pick = (event: PointerEvent<SVGSVGElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    if (box.width <= 0) return;
    const i = Math.floor(((event.clientX - box.left) / box.width) * n);
    setActive(Math.min(n - 1, Math.max(0, i)));
  };
  const step = (event: KeyboardEvent<SVGSVGElement>) => {
    const moves: Record<string, (i: number) => number> = {
      ArrowLeft: (i) => i - 1,
      ArrowRight: (i) => i + 1,
      Home: () => 0,
      End: () => n - 1,
    };
    const move = moves[event.key];
    if (!move) return;
    event.preventDefault();
    setActive((i) => Math.min(n - 1, Math.max(0, move(i ?? n - 1))));
  };

  return (
    <div className="relative h-8">
      <svg
        role="img"
        aria-label={`${label}, ${unit} per day. Busiest day ${fmtDay(dates[peak] ?? '')}: ${fmtCompact(values[peak] ?? 0)} ${unit}.`}
        tabIndex={0}
        viewBox={`0 0 ${n} 100`}
        preserveAspectRatio="none"
        className="block h-full w-full cursor-crosshair rounded-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-400"
        onPointerMove={pick}
        onPointerLeave={() => setActive(null)}
        onFocus={() => setActive((i) => i ?? n - 1)}
        onBlur={() => setActive(null)}
        onKeyDown={step}
      >
        {values.map((v, i) => {
          // Anything served stays visible next to a much busier day.
          const h = v > 0 ? Math.max(3, (v / max) * 100) : 0;
          return (
            <rect
              key={dates[i]}
              x={i + gap / 2}
              y={100 - h}
              width={1 - gap}
              height={h}
              fill={i === active ? BAR_ACTIVE : BAR}
              // The first and last days are cut by the window, as in the daily chart.
              fillOpacity={i === 0 || i === n - 1 ? 0.35 : 1}
            />
          );
        })}
      </svg>
      {active !== null && (
        <div
          role="status"
          className="pointer-events-none absolute bottom-full z-10 mb-1 -translate-x-1/2 whitespace-nowrap rounded-md border border-gray-200 bg-white px-2 py-1 text-xs shadow-sm"
          style={{ left: `${((active + 0.5) / n) * 100}%` }}
        >
          <span className="font-semibold tabular-nums text-gray-950">
            {fmtCompact(values[active])} {values[active] === 1 ? unit.slice(0, -1) : unit}
          </span>{' '}
          <span className="text-gray-500">
            {fmtDay(dates[active])}
            {active === 0 || active === n - 1 ? ' · partial day' : ''}
          </span>
        </div>
      )}
    </div>
  );
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 20 20"
      className={`h-5 w-5 shrink-0 text-gray-500 transition-transform ${open ? 'rotate-90' : ''}`}
    >
      <path d="M7.5 5 12.5 10 7.5 15" fill="none" stroke="currentColor" strokeWidth="2" />
    </svg>
  );
}

/** Requests and tokens per day for each model, folded until the reader opens it. */
export function ModelTrafficSection({
  models,
  dates,
}: {
  models: NonNullable<PublicStats['models']>;
  dates: string[];
}): JSX.Element {
  const [open, setOpen] = useState(false);
  const [metric, setMetric] = useState<Metric>('tokens');
  const rows: { key: string; label: string; series: ModelTraffic }[] = [
    ...[...models.items]
      .sort((a, b) => b[metric] - a[metric] || a.name.localeCompare(b.name))
      .map((m) => ({ key: m.name, label: m.name, series: m })),
    ...(models.other ? [{ key: '__other__', label: OTHER_LABEL, series: models.other }] : []),
  ];
  const all = (m: Metric) => rows.reduce((sum, r) => sum + r.series[m], 0);
  const top = [...models.items].sort((a, b) => b.tokens - a.tokens)[0];

  return (
    <section className="space-y-4" aria-labelledby="stats-models">
      <div className="space-y-1">
        <h2 id="stats-models" className="font-serif text-2xl font-semibold text-gray-900">
          <button
            type="button"
            aria-expanded={open}
            aria-controls="stats-models-body"
            onClick={() => setOpen((o) => !o)}
            className="-ml-1 inline-flex items-center gap-1.5 rounded-md px-1 text-left hover:text-gray-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-400"
          >
            <Chevron open={open} />
            Traffic by model
          </button>
        </h2>
        <p className="max-w-3xl text-sm text-gray-600">
          Requests and tokens per day for each of the {models.items.length} models we offer
          {models.other ? ', with every other model counted together' : ''}.
          {top && all('tokens') > 0
            ? ` ${top.name} served ${fmtPercent(top.tokens / all('tokens'))} of all tokens.`
            : ''}
        </p>
      </div>
      <div id="stats-models-body" hidden={!open}>
        {open && (
          <div className="min-w-0 rounded-2xl border border-gray-200 bg-white p-5 shadow-sm sm:p-6">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-sm font-semibold text-gray-900">
                {metric === 'tokens' ? 'Tokens per day' : 'Requests per day'}, by model
              </h3>
              <div
                className="inline-flex rounded-lg border border-gray-200 p-0.5"
                role="group"
                aria-label="Measure"
              >
                {(['tokens', 'requests'] as const).map((m) => (
                  <button
                    key={m}
                    type="button"
                    aria-pressed={metric === m}
                    onClick={() => setMetric(m)}
                    className={`rounded-md px-2.5 py-1 text-xs font-medium ${
                      metric === m ? 'bg-gray-900 text-white' : 'text-gray-600 hover:bg-gray-100'
                    }`}
                  >
                    {m === 'tokens' ? 'Tokens' : 'Requests'}
                  </button>
                ))}
              </div>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-200 text-left text-xs uppercase tracking-wide text-gray-500">
                    <th className="py-2 pr-4 font-medium">Model</th>
                    <th className="py-2 pr-4 text-right font-medium">
                      {metric === 'tokens' ? 'Tokens' : 'Requests'}
                    </th>
                    <th className="hidden py-2 pr-4 text-right font-medium sm:table-cell">Share</th>
                    <th className="w-1/2 py-2 font-medium">Per day</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.key} className="border-b border-gray-100">
                      <td
                        className={`py-2 pr-4 ${r.key === '__other__' ? 'text-gray-600' : 'font-mono text-[13px] text-gray-900'}`}
                      >
                        {r.label}
                      </td>
                      <td className="py-2 pr-4 text-right tabular-nums text-gray-700">
                        {fmtCompact(r.series[metric])}
                      </td>
                      <td className="hidden py-2 pr-4 text-right tabular-nums text-gray-600 sm:table-cell">
                        {fmtPercent(all(metric) ? r.series[metric] / all(metric) : 0)}
                      </td>
                      <td className="py-2">
                        <DailyBars
                          label={r.label}
                          dates={dates}
                          values={daily(r.series, metric)}
                          metric={metric}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="mt-3 text-xs text-gray-500">
              Each row is scaled to that model&apos;s busiest day. The first and last days are
              partial and drawn lighter. Hover or focus a row&apos;s bars for a day&apos;s value.
            </p>
          </div>
        )}
      </div>
    </section>
  );
}
