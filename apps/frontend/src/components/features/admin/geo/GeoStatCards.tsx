import type { CurrentHourStats } from './geoMath';
import { CONTINENT_NAMES } from './geoMath';

const numberFormat = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });

function formatCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 10_000) return `${Math.round(value / 1_000)}k`;
  return numberFormat.format(Math.round(value));
}

function formatPercent(value: number): string {
  const percent = value * 100;
  return `${percent.toFixed(percent >= 9.5 ? 0 : 1)}%`;
}

function StatCard({
  title,
  value,
  detail,
  titleText,
}: {
  title: string;
  value: string;
  detail: string;
  titleText?: string;
}) {
  return (
    <div
      aria-description={titleText}
      aria-label={titleText ? `${title}: ${value}` : undefined}
      className="rounded-xl border border-gray-200 bg-white p-4"
      role={titleText ? 'group' : undefined}
      title={titleText}
    >
      <p className="text-xs font-medium text-gray-500">{title}</p>
      <p className="mt-1 text-2xl font-semibold tracking-tight text-gray-900">{value}</p>
      <p className="mt-1 text-xs text-gray-500">{detail}</p>
    </div>
  );
}

export function GeoStatCards({ stats }: { stats: CurrentHourStats }) {
  const top = stats.topContinent;
  const remainder = stats.continentTotals
    .slice(1, 4)
    .map(({ continent, fraction }) => `${continent} ${formatPercent(fraction)}`)
    .join(' · ');
  const originMixDetail = top
    ? remainder || 'single-continent hour'
    : stats.totalRequests
      ? 'no located volume for selected metric'
      : 'no demand this hour';

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <StatCard
        title="Requests in hour"
        value={formatCount(stats.totalRequests)}
        detail={`${stats.activeCountries} countries · ${stats.activeContinents} continents`}
      />
      <StatCard
        title="Origin mix"
        value={
          top
            ? `${CONTINENT_NAMES[top.continent] ?? top.continent} ${formatPercent(top.fraction)}`
            : '—'
        }
        detail={originMixDetail}
      />
      <StatCard
        title="Located coverage"
        value={stats.totalRequests ? formatPercent(stats.locatedFraction) : '—'}
        detail={`${formatCount(stats.locatedRequests)} located · ${formatPercent(stats.unlocatedFraction)} unlocated`}
      />
      <StatCard
        title="Demand complementarity"
        value={formatPercent(stats.demandComplementarity)}
        detail={`Peak timing offset across ${stats.observedContinents} continents · not capacity`}
        titleText="How much continent demand peaks are offset across the loaded range: 1 − global peak / sum of continent peaks. This describes demand timing only, not routable capacity."
      />
    </div>
  );
}
