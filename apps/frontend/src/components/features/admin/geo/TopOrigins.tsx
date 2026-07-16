'use client';

import { atlasCountryName, type PreparedAtlas } from './GlobeCanvas';
import { CONTINENT_COLORS, type GeoMetric, type HourOriginSummary } from './geoMath';

const MAX_ROWS = 8;

function formatValue(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 10_000) return `${Math.round(value / 1_000)}k`;
  return Math.round(value).toLocaleString('en-US');
}

function metricLabel(metric: GeoMetric): string {
  return metric === 'tout' ? 'output tokens' : 'requests';
}

/** The selection surface: top request origins for the selected hour, click to focus. */
export function TopOrigins({
  summary,
  atlas,
  hourLabel,
  metric,
  selectedCountry,
  announce,
  onSelect,
}: {
  summary: HourOriginSummary;
  atlas: PreparedAtlas;
  hourLabel: string;
  metric: GeoMetric;
  selectedCountry: string | null;
  announce: boolean;
  onSelect: (country: string) => void;
}) {
  const rows = summary.origins.slice(0, MAX_ROWS);
  const topValue = Math.max(1, rows[0]?.value ?? 0);
  return (
    <aside
      aria-live={announce ? 'polite' : 'off'}
      className="rounded-xl border border-gray-200 bg-white p-3"
    >
      <h3 className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
        Top origins · {hourLabel}
      </h3>
      {rows.length === 0 ? (
        <p className="mt-2 text-xs text-gray-500">
          No requests this hour. Press Play or scrub the timeline.
        </p>
      ) : (
        <ul className="mt-2 space-y-1">
          {rows.map((origin) => {
            const selected = origin.country === selectedCountry;
            return (
              <li key={origin.country}>
                <button
                  type="button"
                  aria-pressed={selected}
                  className={`block w-full rounded-lg px-1.5 py-1 text-left transition ${
                    selected ? 'bg-gray-100' : 'hover:bg-gray-50'
                  }`}
                  onClick={() => onSelect(origin.country)}
                >
                  <span className="flex items-center gap-2 text-[13px] text-gray-800">
                    <span
                      aria-hidden="true"
                      className="h-2 w-2 shrink-0 rounded-full"
                      style={{
                        backgroundColor:
                          CONTINENT_COLORS[origin.continent] ?? CONTINENT_COLORS['?'],
                      }}
                    />
                    <span className="min-w-0 flex-1 truncate">
                      {atlasCountryName(atlas, origin.country)}
                    </span>
                    <span className="font-semibold tabular-nums">
                      {formatValue(origin.value)}
                    </span>
                  </span>
                  <span
                    aria-hidden="true"
                    className="ml-4 mt-1 block h-0.5 rounded bg-gray-300"
                    style={{ width: `${Math.round((88 * origin.value) / topValue) + 6}%` }}
                  />
                </button>
              </li>
            );
          })}
        </ul>
      )}
      {rows.length > 0 && (
        <p className="mt-2 text-[11px] text-gray-400">{metricLabel(metric)} this hour</p>
      )}
    </aside>
  );
}
