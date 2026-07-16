import { describe, expect, it } from 'vitest';

import type { GeoAnalyticsResponse, GeoBucketColumn, GeoBucketRow } from '@/lib/api/admin';
import {
  CONTINENT_COLORS,
  buildColumnIndex,
  buildContinentSeries,
  buildCountryContinentMap,
  currentHourStats,
  rangeDemandComplementarity,
} from './geoMath';

const BUCKET_COLS: GeoBucketColumn[] = [
  'c',
  'cc',
  'cont',
  'n',
  'err',
  'users',
  'tin',
  'tout',
  'gs',
  'p50',
  'p90',
];

function bucket(
  country: string,
  continent: string,
  requests: number,
  outputTokens = requests * 10,
  computeSeconds = requests * 2,
): GeoBucketRow {
  return [
    country,
    country.startsWith('?') ? '?' : country.slice(0, 2),
    continent,
    requests,
    0,
    requests,
    requests * 5,
    outputTokens,
    computeSeconds,
    100,
    200,
  ];
}

function makeData(hours: Array<{ b: GeoBucketRow[] }>): GeoAnalyticsResponse {
  return {
    meta: {
      source: 'api_logs',
      generated_at: '2026-07-15T12:00:00Z',
      start: '2026-07-15T00:00:00Z',
      hours: hours.length,
      rows_total: 0,
      rows_with_ip: 0,
      geoip: { country: true, provider: 'dbip-lite', attribution: null },
      degraded: false,
      degraded_reasons: [],
      unmapped_alpha2: [],
      notes: [],
    },
    bucket_cols: BUCKET_COLS,
    hours_index: hours.map((_, index) => `2026-07-15T${String(index).padStart(2, '0')}:00:00Z`),
    hours: hours.map((hour) => ({ b: hour.b })),
  };
}

describe('geo column contract', () => {
  it('builds indices from the response rather than assuming column positions', () => {
    const data = makeData([{ b: [] }]);
    data.bucket_cols = ['n', 'c', 'cont', 'cc', 'err', 'users', 'tin', 'tout', 'gs', 'p50', 'p90'];
    data.hours[0].b = [
      [7, 'SGP', 'AS', 'SG', 0, 1, 10, 20, 2, null, null] as unknown as GeoBucketRow,
    ];

    expect(buildColumnIndex(data.bucket_cols)).toMatchObject({ n: 0, c: 1, cont: 2 });
    expect(buildContinentSeries(data, 'n').get('AS')).toEqual([7]);
    expect(buildCountryContinentMap(data)).toEqual(new Map([['SGP', 'AS']]));
  });

  it('fails clearly when a required column is absent', () => {
    const data = makeData([{ b: [bucket('SGP', 'AS', 7)] }]);
    data.bucket_cols = data.bucket_cols.filter((column) => column !== 'tout');

    expect(() => buildContinentSeries(data, 'tout')).toThrow('missing the tout column');
  });
});

describe('continent series and demand complementarity', () => {
  it('keeps zero-filled hourly gaps and excludes unknown geography', () => {
    const data = makeData([
      { b: [bucket('CHN', 'AS', 10), bucket('USA', 'NA', 2)] },
      { b: [bucket('CHN', 'AS', 2), bucket('USA', 'NA', 10)] },
      { b: [bucket('?', '?', 99)] },
    ]);

    const series = buildContinentSeries(data, 'n');
    expect(series.get('AS')).toEqual([10, 2, 0]);
    expect(series.get('NA')).toEqual([2, 10, 0]);
    expect(series.has('?')).toBe(false);
  });

  it('computes range complementarity from global and continent peaks', () => {
    const data = makeData([
      { b: [bucket('CHN', 'AS', 10), bucket('USA', 'NA', 2)] },
      { b: [bucket('CHN', 'AS', 2), bucket('USA', 'NA', 10)] },
    ]);

    expect(rangeDemandComplementarity(data, 'n')).toEqual({
      complementarity: 0.4,
      continents: 2,
    });
  });

  it('returns zero for empty, single-continent, and zero-demand cases', () => {
    const empty = makeData([]);
    expect(rangeDemandComplementarity(empty, 'n')).toEqual({
      complementarity: 0,
      continents: 0,
    });

    const oneContinent = makeData([
      { b: [bucket('CHN', 'AS', 0)] },
      { b: [bucket('SGP', 'AS', 5)] },
    ]);
    expect(rangeDemandComplementarity(oneContinent, 'n')).toEqual({
      complementarity: 0,
      continents: 1,
    });

    const noDemand = makeData([{ b: [bucket('CHN', 'AS', 0), bucket('USA', 'NA', 0)] }]);
    expect(rangeDemandComplementarity(noDemand, 'n')).toEqual({
      complementarity: 0,
      continents: 2,
    });
  });

  it('keeps continent colors stable independent of demand rank', () => {
    expect(CONTINENT_COLORS.AS).toBe('#3987e5');
    expect(CONTINENT_COLORS.NA).toBe('#199e70');
    expect(CONTINENT_COLORS['?']).toBe('#898781');
  });
});

describe('currentHourStats', () => {
  it('reports only request-origin coverage, mix, and range complementarity', () => {
    const data = makeData([
      {
        b: [bucket('CHN', 'AS', 10), bucket('USA', 'NA', 2), bucket('?', '?', 3)],
      },
      { b: [bucket('CHN', 'AS', 2), bucket('USA', 'NA', 10)] },
    ]);

    const stats = currentHourStats(data, 0, 'n');
    expect(stats.totalRequests).toBe(15);
    expect(stats.locatedRequests).toBe(12);
    expect(stats.locatedFraction).toBeCloseTo(0.8);
    expect(stats.unlocatedFraction).toBeCloseTo(0.2);
    expect(stats.activeCountries).toBe(2);
    expect(stats.activeContinents).toBe(2);
    expect(stats.topContinent).toEqual({ continent: 'AS', value: 10, fraction: 10 / 12 });
    expect(stats.demandComplementarity).toBeCloseTo(0.4);
    expect(stats.observedContinents).toBe(2);
  });

  it('returns finite zero fractions for an empty or out-of-range hour', () => {
    const data = makeData([{ b: [] }]);

    expect(currentHourStats(data, 0, 'gs')).toMatchObject({
      totalRequests: 0,
      locatedFraction: 0,
      unlocatedFraction: 0,
      activeCountries: 0,
      activeContinents: 0,
      topContinent: null,
    });
    expect(currentHourStats(data, 4, 'gs')).toMatchObject({
      totalRequests: 0,
      demandComplementarity: 0,
    });
  });

  it('counts only positive request origins as active', () => {
    const data = makeData([
      {
        b: [bucket('CHN', 'AS', 0), bucket('USA', 'NA', 2), bucket('?', '?', 3)],
      },
    ]);

    expect(currentHourStats(data, 0, 'n')).toMatchObject({
      activeCountries: 1,
      activeContinents: 1,
    });
  });

  it('does not invent an origin mix when the selected metric is zero', () => {
    const data = makeData([
      {
        b: [bucket('CHN', 'AS', 4, 0), bucket('USA', 'NA', 2, 0)],
      },
    ]);

    expect(currentHourStats(data, 0, 'tout')).toMatchObject({
      totalRequests: 6,
      continentTotals: [],
      topContinent: null,
    });
  });
});
