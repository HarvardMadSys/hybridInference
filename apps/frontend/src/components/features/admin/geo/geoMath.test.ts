import { describe, expect, it } from 'vitest';

import type {
  GeoAnalyticsResponse,
  GeoBucketColumn,
  GeoBucketRow,
  GeoFlowRow,
} from '@/lib/api/admin';
import {
  CONTINENT_COLORS,
  aggregateFlowsByCountryProvider,
  buildColumnIndex,
  buildContinentSeries,
  currentHourStats,
  rangePooling,
  transferableAt,
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

function flow(
  country: string,
  provider: string,
  requests: number,
  endpoint = provider,
): GeoFlowRow {
  return [country, provider, endpoint, requests];
}

function makeData(hours: Array<{ b: GeoBucketRow[]; f?: GeoFlowRow[] }>): GeoAnalyticsResponse {
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
    flow_cols: ['c', 'p', 'e', 'n'],
    providers: [
      {
        id: 'vllm',
        label: 'Local cluster',
        kind: 'local',
        region: 'us-east',
        cont: 'NA',
        coord: [-71.09, 42.36],
      },
      {
        id: 'openrouter',
        label: 'OpenRouter API',
        kind: 'remote_api',
        region: null,
        cont: null,
        coord: null,
      },
    ],
    hours_index: hours.map((_, index) => `2026-07-15T${String(index).padStart(2, '0')}:00:00Z`),
    hours: hours.map((hour) => ({ b: hour.b, f: hour.f ?? [] })),
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
  });

  it('fails clearly when a required column is absent', () => {
    const data = makeData([{ b: [bucket('SGP', 'AS', 7)] }]);
    data.bucket_cols = data.bucket_cols.filter((column) => column !== 'tout');

    expect(() => buildContinentSeries(data, 'tout')).toThrow('missing the tout column');
  });
});

describe('continent series and pooling', () => {
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

  it('computes range pooling from global and regional peaks', () => {
    const data = makeData([
      { b: [bucket('CHN', 'AS', 10), bucket('USA', 'NA', 2)] },
      { b: [bucket('CHN', 'AS', 2), bucket('USA', 'NA', 10)] },
    ]);

    expect(rangePooling(data, 'n')).toEqual({ pooling: 0.4, conts: 2 });
    expect(transferableAt(data, 0, 'n')).toBeCloseTo(1 / 3);
    expect(transferableAt(data, 1, 'n')).toBeCloseTo(1 / 3);
  });

  it('returns zero for empty, single-continent, invalid-hour, and zero-demand cases', () => {
    const empty = makeData([]);
    expect(rangePooling(empty, 'n')).toEqual({ pooling: 0, conts: 0 });
    expect(transferableAt(empty, 0, 'n')).toBe(0);

    const oneContinent = makeData([
      { b: [bucket('CHN', 'AS', 0)] },
      { b: [bucket('SGP', 'AS', 5)] },
    ]);
    expect(rangePooling(oneContinent, 'n')).toEqual({ pooling: 0, conts: 1 });
    expect(transferableAt(oneContinent, -1, 'n')).toBe(0);
    expect(transferableAt(oneContinent, 10, 'n')).toBe(0);
    expect(transferableAt(oneContinent, 0, 'n')).toBe(0);
  });

  it('keeps continent colors stable independent of demand rank', () => {
    expect(CONTINENT_COLORS.AS).toBe('#3987e5');
    expect(CONTINENT_COLORS.NA).toBe('#199e70');
    expect(CONTINENT_COLORS['?']).toBe('#898781');
  });
});

describe('currentHourStats', () => {
  it('reports origin mix, unlocated traffic, serving split, and range opportunity', () => {
    const data = makeData([
      {
        b: [bucket('CHN', 'AS', 10), bucket('USA', 'NA', 2), bucket('?', '?', 3)],
        f: [
          flow('CHN', 'vllm', 4),
          flow('USA', 'vllm', 2),
          flow('?', 'vllm', 1),
          flow('CHN', 'openrouter', 5),
        ],
      },
      { b: [bucket('CHN', 'AS', 2), bucket('USA', 'NA', 10)] },
    ]);

    const stats = currentHourStats(data, 0, 'n');
    expect(stats.totalRequests).toBe(15);
    expect(stats.locatedRequests).toBe(12);
    expect(stats.unlocatedFraction).toBeCloseTo(0.2);
    expect(stats.activeCountries).toBe(2);
    expect(stats.topContinent).toEqual({ continent: 'AS', value: 10, fraction: 10 / 12 });
    expect(stats.externalRequests).toBe(5);
    expect(stats.localSameContinentRequests).toBe(2);
    expect(stats.localCrossContinentRequests).toBe(4);
    expect(stats.localUnlocatedRequests).toBe(1);
    expect(stats.servingTotal).toBe(12);
    expect(stats.externalFraction).toBeCloseTo(5 / 12);
    expect(stats.poolingPotential).toBeCloseTo(0.4);
    expect(stats.transferableFraction).toBeCloseTo(1 / 3);
  });

  it('returns finite zero fractions for an empty or out-of-range hour', () => {
    const data = makeData([{ b: [] }]);

    expect(currentHourStats(data, 0, 'gs')).toMatchObject({
      totalRequests: 0,
      unlocatedFraction: 0,
      externalFraction: 0,
      topContinent: null,
    });
    expect(currentHourStats(data, 4, 'gs')).toMatchObject({
      totalRequests: 0,
      transferableFraction: 0,
    });
  });
});

describe('aggregateFlowsByCountryProvider', () => {
  it('combines duplicate provider flows while retaining endpoint totals', () => {
    const data = makeData([
      {
        b: [],
        f: [
          flow('SGP', 'vllm', 3, 'vllm:a'),
          flow('SGP', 'vllm', 7, 'vllm:b'),
          flow('SGP', 'vllm', 2, 'vllm:a'),
          flow('USA', 'openrouter', 4, 'openrouter:x'),
        ],
      },
    ]);

    expect(aggregateFlowsByCountryProvider(data, 0)).toEqual([
      {
        country: 'SGP',
        providerId: 'vllm',
        requests: 12,
        endpoints: [
          { endpointId: 'vllm:b', requests: 7 },
          { endpointId: 'vllm:a', requests: 5 },
        ],
      },
      {
        country: 'USA',
        providerId: 'openrouter',
        requests: 4,
        endpoints: [{ endpointId: 'openrouter:x', requests: 4 }],
      },
    ]);
  });

  it('uses flow_cols dynamically and returns no flows for an invalid hour', () => {
    const data = makeData([{ b: [], f: [flow('SGP', 'vllm', 3, 'vllm:a')] }]);
    data.flow_cols = ['n', 'e', 'c', 'p'];
    data.hours[0].f = [[3, 'vllm:a', 'SGP', 'vllm'] as unknown as GeoFlowRow];

    expect(aggregateFlowsByCountryProvider(data, 0)[0]).toMatchObject({
      country: 'SGP',
      providerId: 'vllm',
      requests: 3,
    });
    expect(aggregateFlowsByCountryProvider(data, 2)).toEqual([]);
  });
});
