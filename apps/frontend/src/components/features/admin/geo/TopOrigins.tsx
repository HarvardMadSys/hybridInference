'use client';

import { atlasCountryName, type PreparedAtlas } from './GlobeCanvas';
import { ORIGIN_COLORS, type GeoMetric, type OriginSummary } from './geoMath';

const MAX_ROWS = 8;

function formatValue(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 10_000) return `${Math.round(value / 1_000)}k`;
  return Math.round(value).toLocaleString('en-US');
}

/** A share of the whole scope total; small shares keep one decimal so they never read as 0%. */
function formatShare(share: number): string {
  if (!(share > 0)) return '0%';
  if (share < 0.001) return '<0.1%';
  if (share < 0.1) return `${(share * 100).toFixed(1)}%`;
  return `${Math.round(share * 100)}%`;
}

function metricLabel(metric: GeoMetric): string {
  return metric === 'tout' ? 'output tokens' : 'requests';
}

/** The selection surface: top request origins for the active overview or hour, click to focus. */
export function TopOrigins({
  summary,
  atlas,
  label,
  metric,
  mode,
  selectedCountry,
  announce,
  onSelect,
}: {
  summary: OriginSummary;
  atlas: PreparedAtlas;
  label: string;
  metric: GeoMetric;
  mode: 'window' | 'hour';
  selectedCountry: string | null;
  announce: boolean;
  onSelect: (country: string) => void;
}) {
  const rows = summary.origins.slice(0, MAX_ROWS);
  // Shares are of everything in scope, unlocated traffic included, so they
  // agree with the hero total rather than summing to 100% over the list.
  const total = summary.totalValue;
  const locatedValue = summary.origins.reduce((sum, origin) => sum + origin.value, 0);
  const unlocatedShare = total > 0 ? Math.max(0, total - locatedValue) / total : 0;
  const unit = metricLabel(metric);
  const scope = mode === 'window' ? 'in selected window' : 'this hour';
  return (
    <aside aria-live={announce ? 'polite' : 'off'} className="flex min-w-0 flex-col">
      <h3 className="text-[11px] font-medium uppercase tracking-[0.14em] text-slate-500">
        Top origins · {label}
      </h3>
      {rows.length === 0 ? (
        <p className="mt-3 text-[13px] leading-relaxed text-slate-400">
          {summary.totalRequests > 0
            ? `No located origins ${mode === 'window' ? 'in this window' : 'this hour'} — origins are unknown for this traffic.`
            : mode === 'window'
              ? 'No requests in this window. Select an hour to inspect its detail.'
              : 'No requests this hour. Press Play or scrub the timeline.'}
        </p>
      ) : (
        <ol className="-mx-2 mt-2.5 space-y-px">
          {rows.map((origin, index) => {
            const selected = origin.country === selectedCountry;
            const share = total > 0 ? Math.min(1, origin.value / total) : 0;
            const name = atlasCountryName(atlas, origin.country);
            return (
              <li key={origin.country}>
                <button
                  type="button"
                  aria-label={`${name}: ${formatValue(origin.value)} ${unit}, ${formatShare(share)} of all ${unit} ${scope}`}
                  aria-pressed={selected}
                  className={`block w-full rounded-md px-2 pb-1.5 pt-1 text-left transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/80 ${
                    selected ? 'bg-white/10' : 'hover:bg-white/[0.05]'
                  }`}
                  onClick={() => onSelect(origin.country)}
                >
                  <span className="flex items-baseline gap-2 text-[13.5px]">
                    <span
                      aria-hidden="true"
                      className="w-4 shrink-0 text-[11px] tabular-nums text-slate-500"
                    >
                      {index + 1}
                    </span>
                    <span
                      className={`min-w-0 flex-1 truncate ${selected ? 'text-white' : 'text-slate-300'}`}
                    >
                      {name}
                    </span>
                    <span className="text-xs tabular-nums text-slate-500">
                      {formatValue(origin.value)}
                    </span>
                    <span
                      className={`w-10 shrink-0 text-right tabular-nums ${
                        selected ? 'text-white' : 'text-slate-400'
                      }`}
                    >
                      {formatShare(share)}
                    </span>
                  </span>
                  <span
                    aria-hidden="true"
                    className="ml-6 mt-1 block h-0.5 overflow-hidden rounded-full"
                  >
                    <span
                      className="block h-full rounded-full"
                      data-testid="origin-share-bar"
                      style={{
                        backgroundColor: selected ? ORIGIN_COLORS.selected : ORIGIN_COLORS.mark,
                        opacity: selected ? 0.9 : 0.55,
                        // A sliver keeps a tiny share visible without overstating it.
                        width: `${Math.max(1.5, share * 100)}%`,
                      }}
                    />
                  </span>
                </button>
              </li>
            );
          })}
        </ol>
      )}
      {rows.length > 0 && (
        <p className="mt-3 text-[11px] leading-snug text-slate-500">
          % of all {formatValue(total)} {unit} {scope}
          {unlocatedShare > 0 ? ` · ${formatShare(unlocatedShare)} unlocated` : ''}
        </p>
      )}
    </aside>
  );
}
