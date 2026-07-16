// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { GeoAnalyticsResponse } from '@/lib/api/admin';
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
    bucket_cols: ['c', 'cc', 'cont', 'n', 'err', 'users', 'tin', 'tout', 'gs', 'p50', 'p90'],
    hours_index: hoursIndex,
    hours: hoursIndex.map((_, index) => ({
      b: [['USA', 'US', 'NA', index + 1, 0, 1, 0, index + 1, index + 1, null, null]],
    })),
  };
}

afterEach(cleanup);

describe('TrafficRibbon', () => {
  it('leaves the rest of the fixed UTC day empty for a partial day', () => {
    const data = response(
      Array.from(
        { length: 11 },
        (_, hour) => `2026-07-15T${String(hour).padStart(2, '0')}:00:00+00:00`,
      ),
    );
    const { container } = render(
      <TrafficRibbon data={data} hourIndex={10} metric="n" onHourChange={vi.fn()} />,
    );
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
    render(<TrafficRibbon data={data} hourIndex={3} metric="n" onHourChange={onHourChange} />);

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
    render(<TrafficRibbon data={data} hourIndex={2} metric="n" onHourChange={onHourChange} />);

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
});
