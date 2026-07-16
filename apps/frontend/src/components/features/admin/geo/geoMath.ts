import type { GeoAnalyticsResponse, GeoBucketRow, GeoMetric } from '@/lib/api/admin';

export type { GeoMetric } from '@/lib/api/admin';

export const UNKNOWN_CONTINENT = '?';

export const CONTINENT_NAMES: Readonly<Record<string, string>> = {
  AS: 'Asia',
  EU: 'Europe',
  NA: 'N. America',
  SA: 'S. America',
  AF: 'Africa',
  OC: 'Oceania',
  AN: 'Antarctica',
};

// Fixed color slots: a continent keeps its color even when its rank changes.
export const CONTINENT_COLORS: Readonly<Record<string, string>> = {
  AS: '#3987e5',
  NA: '#199e70',
  EU: '#c98500',
  SA: '#008300',
  AF: '#9085e9',
  OC: '#e66767',
  AN: '#898781',
  [UNKNOWN_CONTINENT]: '#898781',
};

export type GeoColumnIndex = Readonly<Record<string, number>>;

export interface DemandComplementarityResult {
  complementarity: number;
  continents: number;
}

export interface ContinentTotal {
  continent: string;
  value: number;
  fraction: number;
}

export interface CurrentHourStats {
  totalRequests: number;
  locatedRequests: number;
  locatedFraction: number;
  unlocatedFraction: number;
  activeCountries: number;
  activeContinents: number;
  continentTotals: ContinentTotal[];
  topContinent: ContinentTotal | null;
  demandComplementarity: number;
  observedContinents: number;
}

export function buildColumnIndex(columns: readonly string[]): GeoColumnIndex {
  return Object.fromEntries(columns.map((column, index) => [column, index]));
}

function requiredIndex(index: GeoColumnIndex, column: string): number {
  const position = index[column];
  if (position === undefined) {
    throw new Error(`Geo analytics response is missing the ${column} column`);
  }
  return position;
}

function numberAt(row: readonly unknown[], index: number): number {
  const value = row[index];
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, value) : 0;
}

function stringAt(row: readonly unknown[], index: number): string {
  const value = row[index];
  return typeof value === 'string' ? value : '';
}

function fraction(part: number, total: number): number {
  return total > 0 ? part / total : 0;
}

function clampFraction(value: number): number {
  return Math.max(0, Math.min(1, value));
}

function isLocatedContinent(continent: string): boolean {
  return Boolean(continent) && continent !== UNKNOWN_CONTINENT;
}

function metricValue(row: GeoBucketRow, metric: GeoMetric, bucketIndex: GeoColumnIndex): number {
  return numberAt(row, requiredIndex(bucketIndex, metric));
}

/** Build gap-preserving per-continent series for one metric. */
export function buildContinentSeries(
  data: GeoAnalyticsResponse,
  metric: GeoMetric,
): Map<string, number[]> {
  const bucketIndex = buildColumnIndex(data.bucket_cols);
  const continentPosition = requiredIndex(bucketIndex, 'cont');
  requiredIndex(bucketIndex, metric);

  const hourCount = data.hours_index.length;
  const series = new Map<string, number[]>();
  for (let hourIndex = 0; hourIndex < hourCount; hourIndex += 1) {
    for (const row of data.hours[hourIndex]?.b ?? []) {
      const continent = stringAt(row, continentPosition);
      if (!isLocatedContinent(continent)) continue;
      if (!series.has(continent)) series.set(continent, Array(hourCount).fill(0));
      series.get(continent)![hourIndex] += metricValue(row, metric, bucketIndex);
    }
  }
  return series;
}

/** Range-wide demand complementarity: 1 - global peak / sum of continent peaks. */
export function rangeDemandComplementarity(
  data: GeoAnalyticsResponse,
  metric: GeoMetric,
): DemandComplementarityResult {
  const series = buildContinentSeries(data, metric);
  if (series.size < 2 || data.hours_index.length === 0) {
    return { complementarity: 0, continents: series.size };
  }

  const total = Array(data.hours_index.length).fill(0) as number[];
  let sumPeaks = 0;
  for (const values of series.values()) {
    let peak = 0;
    values.forEach((value, hourIndex) => {
      total[hourIndex] += value;
      peak = Math.max(peak, value);
    });
    sumPeaks += peak;
  }

  const globalPeak = Math.max(0, ...total);
  return {
    complementarity: sumPeaks > 0 ? clampFraction(1 - globalPeak / sumPeaks) : 0,
    continents: series.size,
  };
}

/** Map each observed request-origin country to its continent. */
export function buildCountryContinentMap(data: GeoAnalyticsResponse): Map<string, string> {
  const bucketIndex = buildColumnIndex(data.bucket_cols);
  const countryPosition = requiredIndex(bucketIndex, 'c');
  const continentPosition = requiredIndex(bucketIndex, 'cont');
  const result = new Map<string, string>();
  for (const hour of data.hours) {
    for (const row of hour.b) {
      const country = stringAt(row, countryPosition);
      if (country && !result.has(country)) {
        result.set(country, stringAt(row, continentPosition));
      }
    }
  }
  return result;
}

function emptyCurrentHourStats(complementarity: DemandComplementarityResult): CurrentHourStats {
  return {
    totalRequests: 0,
    locatedRequests: 0,
    locatedFraction: 0,
    unlocatedFraction: 0,
    activeCountries: 0,
    activeContinents: 0,
    continentTotals: [],
    topContinent: null,
    demandComplementarity: complementarity.complementarity,
    observedContinents: complementarity.continents,
  };
}

/** Summarize the four stat cards for the selected hour. */
export function currentHourStats(
  data: GeoAnalyticsResponse,
  hourIndex: number,
  metric: GeoMetric,
): CurrentHourStats {
  const complementarity = rangeDemandComplementarity(data, metric);
  const hour = data.hours[hourIndex];
  if (!hour || hourIndex < 0 || hourIndex >= data.hours_index.length) {
    return emptyCurrentHourStats(complementarity);
  }

  const bucketIndex = buildColumnIndex(data.bucket_cols);
  const countryPosition = requiredIndex(bucketIndex, 'c');
  const continentPosition = requiredIndex(bucketIndex, 'cont');
  const requestPosition = requiredIndex(bucketIndex, 'n');

  let totalRequests = 0;
  let locatedRequests = 0;
  const activeCountries = new Set<string>();
  const activeContinents = new Set<string>();
  const valuesByContinent = new Map<string, number>();
  for (const row of hour.b) {
    const requests = numberAt(row, requestPosition);
    const country = stringAt(row, countryPosition);
    const continent = stringAt(row, continentPosition);
    totalRequests += requests;
    if (isLocatedContinent(continent)) {
      locatedRequests += requests;
      const value = metricValue(row, metric, bucketIndex);
      if (value > 0) {
        valuesByContinent.set(continent, (valuesByContinent.get(continent) ?? 0) + value);
      }
      if (requests > 0) activeContinents.add(continent);
    }
    if (requests > 0 && country && !country.startsWith('?')) activeCountries.add(country);
  }

  const continentValueTotal = [...valuesByContinent.values()].reduce(
    (sum, value) => sum + value,
    0,
  );
  const continentTotals = [...valuesByContinent.entries()]
    .map(([continent, value]) => ({
      continent,
      value,
      fraction: fraction(value, continentValueTotal),
    }))
    .sort((a, b) => b.value - a.value || a.continent.localeCompare(b.continent));

  return {
    totalRequests,
    locatedRequests,
    locatedFraction: fraction(locatedRequests, totalRequests),
    unlocatedFraction: fraction(totalRequests - locatedRequests, totalRequests),
    activeCountries: activeCountries.size,
    activeContinents: activeContinents.size,
    continentTotals,
    topContinent: continentTotals[0] ?? null,
    demandComplementarity: complementarity.complementarity,
    observedContinents: complementarity.continents,
  };
}
