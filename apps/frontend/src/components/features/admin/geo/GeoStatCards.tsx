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
    <div className="rounded-xl border border-gray-200 bg-white p-4" title={titleText}>
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

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <StatCard
        title="Requests in hour"
        value={formatCount(stats.totalRequests)}
        detail={`${stats.activeCountries} countries · ${formatPercent(stats.unlocatedFraction)} unlocated`}
      />
      <StatCard
        title="Origin mix"
        value={
          top
            ? `${CONTINENT_NAMES[top.continent] ?? top.continent} ${formatPercent(top.fraction)}`
            : '—'
        }
        detail={remainder || 'single-continent hour'}
      />
      <StatCard
        title="Serving split"
        value={stats.servingTotal ? `${formatPercent(stats.externalFraction)} external` : '—'}
        detail={
          stats.servingTotal
            ? `local same-cont ${formatPercent(stats.localSameContinentFraction)} · cross-cont ${formatPercent(stats.localCrossContinentFraction)}`
            : 'no flows'
        }
      />
      <StatCard
        title="Pooling potential"
        value={formatPercent(stats.poolingPotential)}
        detail={`${stats.continentCount} continents · transferable now ${formatPercent(stats.transferableFraction)}`}
        titleText="Range-wide: 1 − global peak / sum of regional peaks. Transferable uses each continent's range mean as a provisional capacity proxy."
      />
    </div>
  );
}
