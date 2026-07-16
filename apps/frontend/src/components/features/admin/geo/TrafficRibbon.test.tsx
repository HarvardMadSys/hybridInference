// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { GeoAnalyticsResponse } from '@/lib/api/admin';
import { deriveGeoMetricModel } from './geoMath';
import { TrafficRibbon } from './TrafficRibbon';

function response(hoursIndex: string[]): GeoAnalyticsResponse {
  return {
    meta: {
      source: 'api_logs',
      generated_at: '2026-07-15T12:00:00Z',
      start: hoursIndex[0] ?? null,
      hours: hoursIndex.length,
      rows_total: hoursIndex.length,
      rows_with_ip: hoursIndex.length,
      geoip: { country: true, provider: 'dbip-lite', attribution: null },
      degraded: false,
      degraded_reasons: [],
      unmapped_alpha2: [],
      notes: [],
    },
    bucket_cols: ['c', 'cont', 'n', 'tout'],
    hours_index: hoursIndex,
    hours: hoursIndex.map((_, index) => ({
      b: [['USA', 'NA', index + 1, index + 1]],
    })),
  };
}

afterEach(cleanup);

function renderRibbon(data: GeoAnalyticsResponse, hourIndex: number, onHourChange = vi.fn()) {
  const metricModel = deriveGeoMetricModel(data, 'n');
  const view = render(
    <TrafficRibbon
      data={data}
      hourIndex={hourIndex}
      metricModel={metricModel}
      onHourChange={onHourChange}
    />,
  );
  return {
    ...view,
    metricModel,
    rerenderAt(nextHourIndex: number) {
      view.rerender(
        <TrafficRibbon
          data={data}
          hourIndex={nextHourIndex}
          metricModel={metricModel}
          onHourChange={onHourChange}
        />,
      );
    },
  };
}

describe('TrafficRibbon', () => {
  it('leaves the rest of the fixed UTC day empty for a partial day', () => {
    const data = response(
      Array.from(
        { length: 11 },
        (_, hour) => `2026-07-15T${String(hour).padStart(2, '0')}:00:00+00:00`,
      ),
    );
    const { container } = renderRibbon(data, 10);
    const dailyTimeline = screen.getByRole('slider', {
      name: 'Demand timeline for the selected UTC day',
    });

    const expectedTenOClockX = 8 + (10 / 24) * (1_000 - 48 - 8);
    expect(dailyTimeline).toHaveAttribute('aria-valuemin', '0');
    expect(dailyTimeline).toHaveAttribute('aria-valuemax', '10');
    expect(Number(screen.getByTestId('selected-hour-marker').getAttribute('x1'))).toBeCloseTo(
      expectedTenOClockX,
    );

    const path = container.querySelector('path[data-continent="NA"]');
    const endpoint = path?.getAttribute('d')?.match(/(-?\d+(?:\.\d+)?),-?\d+(?:\.\d+)?$/);
    expect(Number(endpoint?.[1])).toBeCloseTo(expectedTenOClockX, 2);
    expect(Number(endpoint?.[1])).toBeLessThan(500);
  });

  it('maps a chart click to the nearest real bucket on the selected UTC day', () => {
    const data = response([
      '2026-07-14T23:00:00+00:00',
      '2026-07-15T00:00:00+00:00',
      '2026-07-15T05:00:00+00:00',
      '2026-07-15T11:00:00+00:00',
    ]);
    const onHourChange = vi.fn();
    renderRibbon(data, 3, onHourChange);

    const timeline = screen.getByRole('slider', {
      name: 'Demand timeline for the selected UTC day',
    });
    timeline.getBoundingClientRect = () => ({ left: 0, width: 1_000 }) as DOMRect;
    const fiveOClockX = 8 + (5 / 24) * (1_000 - 48 - 8);
    fireEvent.click(timeline, { clientX: fiveOClockX });

    expect(onHourChange).toHaveBeenCalledWith(2);
    expect(
      document.querySelector('path[data-continent="NA"]')?.getAttribute('d')?.match(/M/g),
    ).toHaveLength(3);
  });

  it('exposes UTC value text and supports keyboard selection within the day', () => {
    const data = response([
      '2026-07-15T00:00:00+00:00',
      '2026-07-15T05:00:00+00:00',
      '2026-07-15T11:00:00+00:00',
    ]);
    const onHourChange = vi.fn();
    renderRibbon(data, 2, onHourChange);

    const dailyTimeline = screen.getByRole('slider', {
      name: 'Demand timeline for the selected UTC day',
    });
    const loadedRange = screen.getByRole('slider', {
      name: 'Selected hour across loaded range',
    });
    expect(dailyTimeline).toHaveAttribute('aria-valuetext', '2026-07-15 11:00 UTC');
    expect(loadedRange).toHaveAttribute('aria-valuetext', '2026-07-15 11:00 UTC');

    fireEvent.keyDown(dailyTimeline, { key: 'ArrowLeft' });
    fireEvent.keyDown(dailyTimeline, { key: 'Home' });

    expect(onHourChange).toHaveBeenNthCalledWith(1, 1);
    expect(onHourChange).toHaveBeenNthCalledWith(2, 0);
  });

  it('keeps the range-wide p99 scale stable across UTC days and clamps outliers', () => {
    const data = response(
      Array.from({ length: 101 }, (_, index) =>
        new Date(Date.UTC(2026, 6, 1, index)).toISOString(),
      ),
    );
    data.hours = Array.from({ length: 101 }, (_, index) => ({
      b: [['USA', 'NA', index === 100 ? 10_000 : 10, 0]],
    }));
    const { metricModel, rerenderAt } = renderRibbon(data, 0);
    const pathData = () => document.querySelector('path[data-continent="NA"]')?.getAttribute('d');

    expect(metricModel.continentHourP99).toBe(10);
    const firstDayPath = pathData();
    rerenderAt(24);
    expect(pathData()).toBe(firstDayPath);
    rerenderAt(100);
    expect(pathData()).toMatch(/,10(?:\D|$)/);
    expect(screen.getByTestId('ribbon-scale-note')).toHaveTextContent(
      'Absolute scale · capped at range p99: 10 requests/continent-hour',
    );
  });

  it('keeps three capped continent labels separated inside the chart', () => {
    const data = response(['2026-07-15T00:00:00+00:00']);
    data.hours = [
      {
        b: [
          ['CHN', 'AS', 10, 0],
          ['USA', 'NA', 10, 0],
          ['DEU', 'EU', 10, 0],
        ],
      },
    ];
    renderRibbon(data, 0);

    const labelYs = ['AS', 'NA', 'EU']
      .map((continent) =>
        [...document.querySelectorAll('svg text')].find((label) => label.textContent === continent),
      )
      .map((label) => Number(label?.getAttribute('y')))
      .sort((a, b) => a - b);

    expect(labelYs).toHaveLength(3);
    expect(labelYs[0]).toBeGreaterThanOrEqual(16);
    expect(labelYs[2]).toBeLessThanOrEqual(80);
    expect(labelYs[1] - labelYs[0]).toBeGreaterThanOrEqual(11);
    expect(labelYs[2] - labelYs[1]).toBeGreaterThanOrEqual(11);
  });
});
