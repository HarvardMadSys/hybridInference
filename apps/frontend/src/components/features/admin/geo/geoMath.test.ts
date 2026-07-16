import { describe, expect, it } from 'vitest';

import type { GeoAnalyticsResponse, GeoBucketRow } from '@/lib/api/admin';
import {
  CONTINENT_COLORS,
  buildColumnIndex,
  buildContinentSeries,
  buildCountryContinentMap,
  demandWeightedRotation,
  deriveGeoMetricModel,
  hourOriginSummary,
  positiveNearestRankPercentile,
  rotationForCoordinate,
} from './geoMath';

const BUCKET_COLS: GeoAnalyticsResponse['bucket_cols'] = ['c', 'cont', 'n', 'tout'];

function bucket(
  country: string,
  continent: string,
  requests: number,
  outputTokens = requests * 10,
): GeoBucketRow {
  return [country, continent, requests, outputTokens];
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
  it('builds indices for the compact request-demand contract', () => {
    const data = makeData([{ b: [bucket('SGP', 'AS', 7, 20)] }]);

    expect(buildColumnIndex(data.bucket_cols)).toEqual({ c: 0, cont: 1, n: 2, tout: 3 });
    expect(buildContinentSeries(data, 'n').get('AS')).toEqual([7]);
    expect(buildCountryContinentMap(data)).toEqual(new Map([['SGP', 'AS']]));
  });
});

describe('continent series and fixed scales', () => {
  it('uses positive nearest-rank p99 values and keeps globe and timeline caps separate', () => {
    expect(positiveNearestRankPercentile([0, -1, Number.NaN, 1, 2, 100], 0.5)).toBe(2);

    const data = makeData([
      {
        b: [bucket('CHN', 'AS', 5), bucket('SGP', 'AS', 5), bucket('?', '?', 50_000)],
      },
    ]);
    const model = deriveGeoMetricModel(data, 'n');

    expect(model.countryHourP99).toBe(5);
    expect(model.continentHourP99).toBe(10);
    expect(model.continentSeries.get('AS')).toEqual([10]);
  });

  it('caps a one-percent range outlier without treating zero volume as positive', () => {
    const data = makeData(
      Array.from({ length: 101 }, (_, index) => ({
        b: [bucket('CHN', 'AS', index === 100 ? 10_000 : 10)],
      })),
    );
    const model = deriveGeoMetricModel(data, 'n');

    expect(model.countryHourP99).toBe(10);
    expect(model.continentHourP99).toBe(10);
    expect(deriveGeoMetricModel(makeData([{ b: [bucket('CHN', 'AS', 0)] }]), 'n')).toMatchObject({
      countryHourP99: 0,
      continentHourP99: 0,
    });
  });

  it('includes plottable countries with an unknown continent in the globe cap only', () => {
    const model = deriveGeoMetricModel(makeData([{ b: [bucket('USA', '?', 7)] }]), 'n');

    expect(model.countryHourP99).toBe(7);
    expect(model.continentHourP99).toBe(0);
    expect(model.continentSeries.has('?')).toBe(false);
  });

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

  it('keeps continent colors stable independent of demand rank', () => {
    expect(CONTINENT_COLORS.AS).toBe('#3987e5');
    expect(CONTINENT_COLORS.NA).toBe('#199e70');
    expect(CONTINENT_COLORS['?']).toBe('#898781');
  });
});

describe('hourOriginSummary', () => {
  it('summarizes the hour for the hero line and the top-origins rail', () => {
    const data = makeData([
      {
        b: [bucket('CHN', 'AS', 10), bucket('USA', 'NA', 2), bucket('?', '?', 3)],
      },
      { b: [bucket('CHN', 'AS', 2), bucket('USA', 'NA', 10)] },
    ]);

    const summary = hourOriginSummary(data, 0, deriveGeoMetricModel(data, 'n'));
    expect(summary.totalRequests).toBe(15);
    expect(summary.totalValue).toBe(15);
    expect(summary.activeCountries).toBe(2);
    expect(summary.unlocatedFraction).toBeCloseTo(0.2);
    expect(summary.origins.map((origin) => origin.country)).toEqual(['CHN', 'USA']);
    expect(summary.top).toMatchObject({ country: 'CHN', continent: 'AS', requests: 10 });
    expect(summary.topShare).toBeCloseTo(10 / 15);
  });

  it('returns an empty summary for an empty or out-of-range hour', () => {
    const data = makeData([{ b: [] }]);

    const model = deriveGeoMetricModel(data, 'tout');
    expect(hourOriginSummary(data, 0, model)).toMatchObject({
      totalRequests: 0,
      totalValue: 0,
      activeCountries: 0,
      origins: [],
      top: null,
      topShare: 0,
    });
    expect(hourOriginSummary(data, 4, model)).toMatchObject({ totalRequests: 0, top: null });
  });

  it('keeps zero-metric origins listed by requests without inventing a share', () => {
    const data = makeData([
      {
        b: [bucket('CHN', 'AS', 4, 0), bucket('USA', 'NA', 2, 0)],
      },
    ]);

    const summary = hourOriginSummary(data, 0, deriveGeoMetricModel(data, 'tout'));
    expect(summary.totalRequests).toBe(6);
    expect(summary.totalValue).toBe(0);
    expect(summary.origins.map((origin) => origin.country)).toEqual(['CHN', 'USA']);
    expect(summary.topShare).toBe(0);
  });
});

describe('view rotations', () => {
  const COORDINATES = new Map<string, [number, number]>([
    ['USA', [-98, 39]],
    ['CHN', [104, 36]],
  ]);

  it('centers on the request-weighted centroid of the window', () => {
    const data = makeData([{ b: [bucket('USA', 'NA', 10)] }]);

    const rotation = demandWeightedRotation(data, COORDINATES);
    expect(rotation).not.toBeNull();
    expect(rotation![0]).toBeCloseTo(98);
    expect(rotation![1]).toBeCloseTo(-39);
  });

  it('weights the centroid by request volume', () => {
    const data = makeData([{ b: [bucket('USA', 'NA', 99), bucket('CHN', 'AS', 1)] }]);

    const rotation = demandWeightedRotation(data, COORDINATES);
    expect(rotation![0]).toBeGreaterThan(85);
    expect(rotation![0]).toBeLessThan(99);
  });

  it('returns null without located demand and clamps extreme latitudes', () => {
    expect(demandWeightedRotation(makeData([{ b: [bucket('?', '?', 9)] }]), COORDINATES)).toBe(
      null,
    );
    expect(demandWeightedRotation(makeData([{ b: [] }]), COORDINATES)).toBeNull();
    expect(rotationForCoordinate([10, 78])).toEqual([-10, -55]);
    expect(rotationForCoordinate([-98, 39])).toEqual([98, -39]);
  });
});
