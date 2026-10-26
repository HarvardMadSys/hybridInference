import { geoBounds, geoContains, geoDistance } from 'd3';
import type { GeoPermissibleObjects } from 'd3';
import type { GeoAnalyticsResponse, GeoBucketRow, GeoMetric } from '@/lib/api/admin';

export type { GeoMetric } from '@/lib/api/admin';

const UNKNOWN_CONTINENT = '?';

// The night panel: one glowing accent for every view of origin volume, white
// for the selection. Position on the globe already says where traffic comes
// from, so color only has to say how much — and a selected country reads the
// same in the globe, the list and the timeline.
export const ORIGIN_COLORS = {
  /** The panel the whole view sits on. */
  panel: '#070b14',
  /** Volume marks: glow, share bars, the timeline area. */
  mark: '#7dd3fc',
  /** The bright core of a volume mark. */
  core: '#e0f2fe',
  /** The selected country, over the volume marks. */
  selected: '#ffffff',
  /** Land points of the dot-matrix globe. */
  land: '#94a3b8',
} as const;

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

/**
 * Per-hour totals for one metric: every row (unlocated included, so it agrees
 * with the headline total), or only the rows of one country.
 */
export function buildOriginSeries(
  data: GeoAnalyticsResponse,
  metric: GeoMetric,
  country?: string,
): number[] {
  const bucketIndex = buildColumnIndex(data.bucket_cols);
  const countryPosition = requiredIndex(bucketIndex, 'c');
  const valuePosition = requiredIndex(bucketIndex, metric);
  return data.hours_index.map((_, hourIndex) => {
    let total = 0;
    for (const row of data.hours[hourIndex]?.b ?? []) {
      if (country !== undefined && stringAt(row, countryPosition) !== country) continue;
      total += numberAt(row, valuePosition);
    }
    return total;
  });
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
 * Land as a near-equal-area grid of [lon, lat] points — the dot matrix the
 * globe is drawn with. Pure geometry (bounding box, then point-in-polygon), so
 * it needs no canvas and is computed once per atlas.
 */
export function landPoints(
  countries: readonly GeoPermissibleObjects[],
  stepDegrees = 1.6,
): [number, number][] {
  const shapes = countries.map((country) => ({ country, bounds: geoBounds(country) }));
  const points: [number, number][] = [];
  for (let lat = -56; lat <= 82; lat += stepDegrees) {
    const lonStep = stepDegrees / Math.max(0.25, Math.cos((lat * Math.PI) / 180));
    for (let lon = -180; lon < 180; lon += lonStep) {
      const point: [number, number] = [lon, lat];
      const onLand = shapes.some(({ country, bounds: [[west, south], [east, north]] }) => {
        if (lat < south || lat > north) return false;
        // A shape across the antimeridian has west > east.
        const inLongitude = west <= east ? lon >= west && lon <= east : lon >= west || lon <= east;
        return inLongitude && geoContains(country, point);
      });
      if (onLand) points.push(point);
    }
  }
  return points;
}

const DEGREES = 180 / Math.PI;

/**
 * The opening view. The top origin must sit within 58° of the center so it is
 * never pushed onto the rim. Among those views, score the demand in view (full
 * weight within 62°, fading to nothing at 80°) plus how much land is in view —
 * demand alone picks a view that can be half empty sea. Latitude stays between
 * 10° and 45° so it reads as a globe rather than a polar map. Null when no
 * origin is located.
 */
export function openingRotation(
  origins: readonly { country: string; value: number }[],
  coordinates: ReadonlyMap<string, [number, number]>,
  land: readonly [number, number][],
): [number, number] | null {
  const located = origins.filter((origin) => origin.value > 0 && coordinates.has(origin.country));
  if (located.length === 0) return null;
  const anchor = coordinates.get(located[0].country)!;
  const total = located.reduce((sum, origin) => sum + origin.value, 0);
  // Every fourth land point is plenty to compare views.
  const sampledLand = land.filter((_, index) => index % 4 === 0);
  const candidates: { rotation: [number, number]; demand: number; land: number }[] = [];
  for (let lat = 10; lat <= 45; lat += 5) {
    for (let lon = -180; lon < 180; lon += 5) {
      const center: [number, number] = [lon, lat];
      if (geoDistance(center, anchor) * DEGREES > 58) continue;
      let demand = 0;
      for (const origin of located) {
        const distance = geoDistance(center, coordinates.get(origin.country)!) * DEGREES;
        const weight = distance <= 62 ? 1 : distance >= 80 ? 0 : (80 - distance) / 18;
        demand += (origin.value / total) * weight;
      }
      let visibleLand = 0;
      for (const point of sampledLand)
        if (geoDistance(center, point) * DEGREES < 70) visibleLand += 1;
      candidates.push({ rotation: [-lon, -lat], demand, land: visibleLand });
    }
  }
  const maxLand = Math.max(1, ...candidates.map((candidate) => candidate.land));
  let best = candidates[0]?.rotation ?? rotationForCoordinate(anchor);
  let bestScore = -Infinity;
  for (const candidate of candidates) {
    const score = candidate.demand + 0.35 * (candidate.land / maxLand);
    if (score > bestScore) {
      bestScore = score;
      best = candidate.rotation;
    }
  }
  return best;
}

/** Rotation that centers the globe on one coordinate, with a readable latitude clamp. */
export function rotationForCoordinate(coordinate: [number, number]): [number, number] {
  return [-coordinate[0], -Math.max(-55, Math.min(55, coordinate[1]))];
}
