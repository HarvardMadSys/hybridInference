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

export interface GeoMetricModel {
  metric: GeoMetric;
  continentSeries: Map<string, number[]>;
  countryHourP99: number;
  countryWindowP99: number;
  continentHourP99: number;
}

export interface HourOrigin {
  country: string;
  continent: string;
  requests: number;
  value: number;
}

export interface OriginSummary {
  totalRequests: number;
  totalValue: number;
  activeCountries: number;
  unlocatedFraction: number;
  origins: HourOrigin[];
  top: HourOrigin | null;
  topShare: number;
}

export type HourOriginSummary = OriginSummary;

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

/** Nearest-rank percentile over positive finite values only. */
export function positiveNearestRankPercentile(
  values: readonly number[],
  percentile: number,
): number {
  const positive = values
    .filter((value) => Number.isFinite(value) && value > 0)
    .sort((a, b) => a - b);
  if (positive.length === 0) return 0;
  const boundedPercentile = Math.max(0, Math.min(1, percentile));
  const rank = Math.max(1, Math.ceil(boundedPercentile * positive.length));
  return positive[rank - 1];
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

/** Derive stable range-wide series and separate p99 visual caps. */
export function deriveGeoMetricModel(
  data: GeoAnalyticsResponse,
  metric: GeoMetric,
): GeoMetricModel {
  const bucketIndex = buildColumnIndex(data.bucket_cols);
  const countryPosition = requiredIndex(bucketIndex, 'c');
  requiredIndex(bucketIndex, metric);

  const countryHourValues: number[] = [];
  const countryWindowValues = new Map<string, number>();
  for (const hour of data.hours) {
    const byCountry = new Map<string, number>();
    for (const row of hour.b) {
      const country = stringAt(row, countryPosition);
      // A country can still be plotted when the GeoIP record omits its
      // continent. Keep it in the globe's magnitude domain; the continent
      // ribbon independently excludes unknown continent buckets.
      if (!country || country.startsWith('?')) continue;
      const value = metricValue(row, metric, bucketIndex);
      byCountry.set(country, (byCountry.get(country) ?? 0) + value);
      countryWindowValues.set(country, (countryWindowValues.get(country) ?? 0) + value);
    }
    countryHourValues.push(...byCountry.values());
  }

  const continentSeries = buildContinentSeries(data, metric);
  const continentHourValues = [...continentSeries.values()].flat();
  return {
    metric,
    continentSeries,
    countryHourP99: positiveNearestRankPercentile(countryHourValues, 0.99),
    countryWindowP99: positiveNearestRankPercentile([...countryWindowValues.values()], 0.99),
    continentHourP99: positiveNearestRankPercentile(continentHourValues, 0.99),
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

function emptyOriginSummary(): OriginSummary {
  return {
    totalRequests: 0,
    totalValue: 0,
    activeCountries: 0,
    unlocatedFraction: 0,
    origins: [],
    top: null,
    topShare: 0,
  };
}

function originSummaryForHours(
  data: GeoAnalyticsResponse,
  hours: readonly { b: GeoBucketRow[] }[],
  metricModel: GeoMetricModel,
): OriginSummary {
  const bucketIndex = buildColumnIndex(data.bucket_cols);
  const countryPosition = requiredIndex(bucketIndex, 'c');
  const continentPosition = requiredIndex(bucketIndex, 'cont');
  const requestPosition = requiredIndex(bucketIndex, 'n');

  let totalRequests = 0;
  let totalValue = 0;
  let locatedRequests = 0;
  const byCountry = new Map<string, HourOrigin>();
  for (const hour of hours) {
    for (const row of hour.b) {
      const requests = numberAt(row, requestPosition);
      const value = metricValue(row, metricModel.metric, bucketIndex);
      totalRequests += requests;
      totalValue += value;
      const country = stringAt(row, countryPosition);
      if (!country || country.startsWith('?')) continue;
      locatedRequests += requests;
      const entry = byCountry.get(country) ?? {
        country,
        continent: stringAt(row, continentPosition),
        requests: 0,
        value: 0,
      };
      entry.requests += requests;
      entry.value += value;
      byCountry.set(country, entry);
    }
  }

  const origins = [...byCountry.values()]
    .filter((origin) => origin.requests > 0 || origin.value > 0)
    .sort(
      (a, b) => b.value - a.value || b.requests - a.requests || a.country.localeCompare(b.country),
    );
  const top = origins[0] ?? null;
  return {
    totalRequests,
    totalValue,
    activeCountries: origins.filter((origin) => origin.requests > 0).length,
    unlocatedFraction: clampFraction(fraction(totalRequests - locatedRequests, totalRequests)),
    origins,
    top,
    topShare: top && totalValue > 0 ? clampFraction(top.value / totalValue) : 0,
  };
}

/** Per-country demand for the loaded window: feeds the default overview. */
export function windowOriginSummary(
  data: GeoAnalyticsResponse,
  metricModel: GeoMetricModel,
): OriginSummary {
  return originSummaryForHours(data, data.hours, metricModel);
}

/** Per-country demand for one hour: feeds an hour-level drill-down. */
export function hourOriginSummary(
  data: GeoAnalyticsResponse,
  hourIndex: number,
  metricModel: GeoMetricModel,
): HourOriginSummary {
  const hour = data.hours[hourIndex];
  if (!hour || hourIndex < 0 || hourIndex >= data.hours_index.length) {
    return emptyOriginSummary();
  }
  return originSummaryForHours(data, [hour], metricModel);
}

/**
 * Rotation that centers the globe on the request-weighted centroid — of one hour
 * when `hourIndex` is given (what the viewer opens on), else of the whole window.
 */
export function demandWeightedRotation(
  data: GeoAnalyticsResponse,
  coordinates: ReadonlyMap<string, [number, number]>,
  hourIndex?: number,
): [number, number] | null {
  const bucketIndex = buildColumnIndex(data.bucket_cols);
  const countryPosition = requiredIndex(bucketIndex, 'c');
  const requestPosition = requiredIndex(bucketIndex, 'n');
  const hours =
    hourIndex === undefined
      ? data.hours
      : hourIndex >= 0 && hourIndex < data.hours.length
        ? [data.hours[hourIndex]]
        : [];
  let x = 0;
  let y = 0;
  let z = 0;
  let total = 0;
  for (const hour of hours) {
    for (const row of hour.b) {
      const requests = numberAt(row, requestPosition);
      if (requests <= 0) continue;
      const coordinate = coordinates.get(stringAt(row, countryPosition));
      if (!coordinate) continue;
      const latitude = (coordinate[1] * Math.PI) / 180;
      const longitude = (coordinate[0] * Math.PI) / 180;
      x += requests * Math.cos(latitude) * Math.cos(longitude);
      y += requests * Math.cos(latitude) * Math.sin(longitude);
      z += requests * Math.sin(latitude);
      total += requests;
    }
  }
  const magnitude = Math.hypot(x, y, z);
  if (total <= 0 || magnitude === 0) return null;
  return rotationForCoordinate([
    (Math.atan2(y, x) * 180) / Math.PI,
    (Math.asin(z / magnitude) * 180) / Math.PI,
  ]);
}

/** Rotation that centers the globe on one coordinate, with a readable latitude clamp. */
export function rotationForCoordinate(coordinate: [number, number]): [number, number] {
  return [-coordinate[0], -Math.max(-55, Math.min(55, coordinate[1]))];
}
