import type { GeoAnalyticsResponse, GeoBucketRow, GeoFlowRow, GeoMetric } from '@/lib/api/admin';

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

export interface PoolingResult {
  pooling: number;
  conts: number;
}

export interface ContinentTotal {
  continent: string;
  value: number;
  fraction: number;
}

export interface AggregatedFlowEndpoint {
  endpointId: string;
  requests: number;
}

export interface AggregatedCountryProviderFlow {
  country: string;
  providerId: string;
  requests: number;
  endpoints: AggregatedFlowEndpoint[];
}

export interface CurrentHourStats {
  totalRequests: number;
  locatedRequests: number;
  unlocatedFraction: number;
  activeCountries: number;
  continentTotals: ContinentTotal[];
  topContinent: ContinentTotal | null;
  externalRequests: number;
  localSameContinentRequests: number;
  localCrossContinentRequests: number;
  localUnlocatedRequests: number;
  servingTotal: number;
  externalFraction: number;
  localSameContinentFraction: number;
  localCrossContinentFraction: number;
  localUnlocatedFraction: number;
  poolingPotential: number;
  continentCount: number;
  transferableFraction: number;
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

/**
 * Range-wide pooling potential: 1 - global peak / sum of per-continent peaks.
 */
export function rangePooling(data: GeoAnalyticsResponse, metric: GeoMetric): PoolingResult {
  const series = buildContinentSeries(data, metric);
  if (series.size < 2 || data.hours_index.length === 0) {
    return { pooling: 0, conts: series.size };
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
    pooling: sumPeaks > 0 ? clampFraction(1 - globalPeak / sumPeaks) : 0,
    conts: series.size,
  };
}

/**
 * Transferable demand at one hour, using each continent's range mean as the
 * provisional capacity proxy: min(total overflow, total slack) / demand now.
 */
export function transferableAt(
  data: GeoAnalyticsResponse,
  hourIndex: number,
  metric: GeoMetric,
): number {
  const hourCount = data.hours_index.length;
  if (hourIndex < 0 || hourIndex >= hourCount || hourCount === 0) return 0;

  let overflow = 0;
  let slack = 0;
  let demandNow = 0;
  for (const values of buildContinentSeries(data, metric).values()) {
    const mean = values.reduce((sum, value) => sum + value, 0) / hourCount;
    const value = values[hourIndex] ?? 0;
    demandNow += value;
    if (value > mean) overflow += value - mean;
    else slack += mean - value;
  }
  return demandNow > 0 ? clampFraction(Math.min(overflow, slack) / demandNow) : 0;
}

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

/**
 * Combine endpoint-level rows without losing their attribution. The globe
 * draws one country-to-provider arc, while selection detail can still expose
 * every serving endpoint that contributed to it.
 */
export function aggregateFlowsByCountryProvider(
  data: GeoAnalyticsResponse,
  hourIndex: number,
): AggregatedCountryProviderFlow[] {
  const hour = data.hours[hourIndex];
  if (!hour || hourIndex < 0 || hourIndex >= data.hours_index.length) return [];

  const flowIndex = buildColumnIndex(data.flow_cols);
  const countryPosition = requiredIndex(flowIndex, 'c');
  const providerPosition = requiredIndex(flowIndex, 'p');
  const endpointPosition = requiredIndex(flowIndex, 'e');
  const requestPosition = requiredIndex(flowIndex, 'n');
  const grouped = new Map<
    string,
    Map<string, { requests: number; endpoints: Map<string, number> }>
  >();

  for (const row of hour.f) {
    const country = stringAt(row, countryPosition);
    const providerId = stringAt(row, providerPosition);
    const endpointId = stringAt(row, endpointPosition) || providerId;
    const requests = numberAt(row, requestPosition);
    if (!grouped.has(country)) grouped.set(country, new Map());
    const byProvider = grouped.get(country)!;
    if (!byProvider.has(providerId)) {
      byProvider.set(providerId, { requests: 0, endpoints: new Map() });
    }
    const aggregate = byProvider.get(providerId)!;
    aggregate.requests += requests;
    aggregate.endpoints.set(endpointId, (aggregate.endpoints.get(endpointId) ?? 0) + requests);
  }

  const result: AggregatedCountryProviderFlow[] = [];
  for (const [country, byProvider] of grouped) {
    for (const [providerId, aggregate] of byProvider) {
      result.push({
        country,
        providerId,
        requests: aggregate.requests,
        endpoints: [...aggregate.endpoints.entries()]
          .map(([endpointId, requests]) => ({ endpointId, requests }))
          .sort((a, b) => b.requests - a.requests || a.endpointId.localeCompare(b.endpointId)),
      });
    }
  }
  return result.sort(
    (a, b) =>
      b.requests - a.requests ||
      a.country.localeCompare(b.country) ||
      a.providerId.localeCompare(b.providerId),
  );
}

function emptyCurrentHourStats(pooling: PoolingResult): CurrentHourStats {
  return {
    totalRequests: 0,
    locatedRequests: 0,
    unlocatedFraction: 0,
    activeCountries: 0,
    continentTotals: [],
    topContinent: null,
    externalRequests: 0,
    localSameContinentRequests: 0,
    localCrossContinentRequests: 0,
    localUnlocatedRequests: 0,
    servingTotal: 0,
    externalFraction: 0,
    localSameContinentFraction: 0,
    localCrossContinentFraction: 0,
    localUnlocatedFraction: 0,
    poolingPotential: pooling.pooling,
    continentCount: pooling.conts,
    transferableFraction: 0,
  };
}

/** Summarize the four stat cards for the selected hour. */
export function currentHourStats(
  data: GeoAnalyticsResponse,
  hourIndex: number,
  metric: GeoMetric,
): CurrentHourStats {
  const pooling = rangePooling(data, metric);
  const hour = data.hours[hourIndex];
  if (!hour || hourIndex < 0 || hourIndex >= data.hours_index.length) {
    return emptyCurrentHourStats(pooling);
  }

  const bucketIndex = buildColumnIndex(data.bucket_cols);
  const flowIndex = buildColumnIndex(data.flow_cols);
  const countryPosition = requiredIndex(bucketIndex, 'c');
  const continentPosition = requiredIndex(bucketIndex, 'cont');
  const requestPosition = requiredIndex(bucketIndex, 'n');
  const flowCountryPosition = requiredIndex(flowIndex, 'c');
  const flowProviderPosition = requiredIndex(flowIndex, 'p');
  const flowRequestPosition = requiredIndex(flowIndex, 'n');

  let totalRequests = 0;
  let locatedRequests = 0;
  const activeCountries = new Set<string>();
  const valuesByContinent = new Map<string, number>();
  for (const row of hour.b) {
    const requests = numberAt(row, requestPosition);
    const country = stringAt(row, countryPosition);
    const continent = stringAt(row, continentPosition);
    totalRequests += requests;
    if (isLocatedContinent(continent)) {
      locatedRequests += requests;
      valuesByContinent.set(
        continent,
        (valuesByContinent.get(continent) ?? 0) + metricValue(row, metric, bucketIndex),
      );
    }
    if (country && !country.startsWith('?')) activeCountries.add(country);
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

  const providers = new Map(data.providers.map((provider) => [provider.id, provider]));
  const countryContinents = buildCountryContinentMap(data);
  let externalRequests = 0;
  let localSameContinentRequests = 0;
  let localCrossContinentRequests = 0;
  let localUnlocatedRequests = 0;
  for (const row of hour.f as GeoFlowRow[]) {
    const requests = numberAt(row, flowRequestPosition);
    const provider = providers.get(stringAt(row, flowProviderPosition));
    if (!provider || provider.kind !== 'local' || !provider.cont) {
      externalRequests += requests;
      continue;
    }
    const originContinent = countryContinents.get(stringAt(row, flowCountryPosition));
    if (!originContinent || !isLocatedContinent(originContinent)) {
      localUnlocatedRequests += requests;
    } else if (originContinent === provider.cont) {
      localSameContinentRequests += requests;
    } else {
      localCrossContinentRequests += requests;
    }
  }

  const servingTotal =
    externalRequests +
    localSameContinentRequests +
    localCrossContinentRequests +
    localUnlocatedRequests;

  return {
    totalRequests,
    locatedRequests,
    unlocatedFraction: totalRequests > 0 ? 1 - locatedRequests / totalRequests : 0,
    activeCountries: activeCountries.size,
    continentTotals,
    topContinent: continentTotals[0] ?? null,
    externalRequests,
    localSameContinentRequests,
    localCrossContinentRequests,
    localUnlocatedRequests,
    servingTotal,
    externalFraction: fraction(externalRequests, servingTotal),
    localSameContinentFraction: fraction(localSameContinentRequests, servingTotal),
    localCrossContinentFraction: fraction(localCrossContinentRequests, servingTotal),
    localUnlocatedFraction: fraction(localUnlocatedRequests, servingTotal),
    poolingPotential: pooling.pooling,
    continentCount: pooling.conts,
    transferableFraction: transferableAt(data, hourIndex, metric),
  };
}
