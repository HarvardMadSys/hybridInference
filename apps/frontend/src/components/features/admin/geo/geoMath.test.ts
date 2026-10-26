import { geoDistance } from 'd3';
import { describe, expect, it } from 'vitest';

import type { GeoAnalyticsResponse, GeoBucketRow } from '@/lib/api/admin';
import {
  buildColumnIndex,
  buildContinentSeries,
  buildOriginSeries,
  buildCountryContinentMap,
  deriveGeoMetricModel,
  hourOriginSummary,
  landPoints,
  openingRotation,
  positiveNearestRankPercentile,
  rotationForCoordinate,
  windowOriginSummary,
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
});

describe('buildOriginSeries', () => {
  it('totals every row per hour, unlocated included, or only one country', () => {
    const data = makeData([
      { b: [bucket('CHN', 'AS', 10), bucket('USA', 'NA', 2), bucket('?', '?', 3)] },
      { b: [] },
      { b: [bucket('USA', '?', 4, 9)] },
    ]);

    expect(buildOriginSeries(data, 'n')).toEqual([15, 0, 4]);
    expect(buildOriginSeries(data, 'n', 'USA')).toEqual([2, 0, 4]);
    expect(buildOriginSeries(data, 'tout', 'USA')).toEqual([20, 0, 9]);
    expect(buildOriginSeries(data, 'n', 'JPN')).toEqual([0, 0, 0]);
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

describe('windowOriginSummary', () => {
  it('aggregates countries, totals, and unknown traffic across the loaded window', () => {
    const data = makeData([
      {
        b: [bucket('CHN', 'AS', 10, 100), bucket('USA', 'NA', 2, 20), bucket('?', '?', 3, 30)],
      },
      {
        b: [
          bucket('CHN', 'AS', 2, 5),
          bucket('USA', 'NA', 10, 200),
          bucket('JPN', 'AS', 4, 40),
          bucket('?', '?', 5, 50),
        ],
      },
    ]);

    const summary = windowOriginSummary(data, deriveGeoMetricModel(data, 'tout'));

    expect(summary).toMatchObject({
      totalRequests: 36,
      totalValue: 445,
      activeCountries: 3,
    });
    expect(summary.unlocatedFraction).toBeCloseTo(8 / 36);
    expect(summary.origins).toEqual([
      { country: 'USA', continent: 'NA', requests: 12, value: 220 },
      { country: 'CHN', continent: 'AS', requests: 12, value: 105 },
      { country: 'JPN', continent: 'AS', requests: 4, value: 40 },
    ]);
    expect(summary.top).toMatchObject({ country: 'USA', value: 220 });
    expect(summary.topShare).toBeCloseTo(220 / 445);
  });
});

describe('view rotations', () => {
  const COORDINATES = new Map<string, [number, number]>([
    ['USA', [-98, 39]],
    ['CHN', [104, 36]],
  ]);
  const square = (west: number, south: number, east: number, north: number) => ({
    type: 'Feature' as const,
    properties: {},
    // Clockwise, as d3-geo expects for an exterior ring.
    geometry: {
      type: 'Polygon' as const,
      coordinates: [
        [
          [west, south],
          [west, north],
          [east, north],
          [east, south],
          [west, south],
        ],
      ],
    },
  });
  const degreesFromCenter = (rotation: [number, number], coordinate: [number, number]) =>
    (geoDistance([-rotation[0], -rotation[1]], coordinate) * 180) / Math.PI;

  it('samples land as points inside the countries, across the antimeridian too', () => {
    const points = landPoints([square(0, 0, 10, 10), square(170, -10, -170, 0)]);

    expect(points.length).toBeGreaterThan(20);
    for (const [lon, lat] of points) {
      const inFirst = lon >= 0 && lon <= 10 && lat >= 0 && lat <= 10;
      const inSecond = Math.abs(lon) >= 170 && lat >= -10 && lat <= 0;
      expect(inFirst || inSecond).toBe(true);
    }
    expect(points.some(([lon]) => lon > 170)).toBe(true);
    expect(points.some(([lon]) => lon < -170)).toBe(true);
  });

  it('opens with the top origin well inside the disc', () => {
    const rotation = openingRotation(
      [
        { country: 'USA', value: 10 },
        { country: 'CHN', value: 9 },
      ],
      COORDINATES,
      [],
    )!;

    expect(degreesFromCenter(rotation, [-98, 39])).toBeLessThanOrEqual(58);
    expect(-rotation[1]).toBeGreaterThanOrEqual(10);
    expect(-rotation[1]).toBeLessThanOrEqual(45);
  });

  it('prefers a view with land in it over one that is mostly sea', () => {
    // No single view holds all three: the Pacific one keeps China, the
    // Atlantic one keeps Germany. Demand alone picks the Pacific; land in view
    // tips it to the Atlantic, with the top origin still inside.
    const coordinates = new Map(COORDINATES).set('DEU', [10, 51] as [number, number]);
    const origins = [
      { country: 'USA', value: 10 },
      { country: 'CHN', value: 9 },
      { country: 'DEU', value: 8 },
    ];
    const demandOnly = openingRotation(origins, coordinates, [])!;
    const withLand = openingRotation(origins, coordinates, landPoints([square(-10, 0, 40, 55)]))!;

    expect(degreesFromCenter(demandOnly, [104, 36])).toBeLessThan(80);
    expect(degreesFromCenter(demandOnly, [10, 51])).toBeGreaterThan(80);
    expect(degreesFromCenter(withLand, [10, 51])).toBeLessThan(62);
    expect(degreesFromCenter(withLand, [-98, 39])).toBeLessThanOrEqual(58);
  });

  it('returns null without a located origin and clamps extreme latitudes', () => {
    expect(openingRotation([{ country: '?', value: 9 }], COORDINATES, [])).toBeNull();
    expect(openingRotation([{ country: 'USA', value: 0 }], COORDINATES, [])).toBeNull();
    expect(openingRotation([], COORDINATES, [])).toBeNull();
    expect(rotationForCoordinate([10, 78])).toEqual([-10, -55]);
    expect(rotationForCoordinate([-98, 39])).toEqual([98, -39]);
  });
});
