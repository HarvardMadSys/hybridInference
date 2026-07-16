// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { GeoAnalyticsResponse, GeoBucketRow } from '@/lib/api/admin';
import { deriveGeoMetricModel } from './geoMath';
import { WindowTimeline } from './WindowTimeline';

function makeData(hourCount: number): GeoAnalyticsResponse {
  const start = Date.UTC(2026, 6, 14, 23);
  return {
    meta: {
      source: 'api_logs',
      generated_at: '2026-07-16T00:00:00Z',
      start: new Date(start).toISOString(),
      hours: hourCount,
      rows_total: hourCount,
      rows_with_ip: hourCount,
      geoip: { country: true, provider: 'dbip-lite', attribution: null },
      degraded: false,
      degraded_reasons: [],
      unmapped_alpha2: [],
      notes: [],
    },
    bucket_cols: ['c', 'cont', 'n', 'tout'],
    hours_index: Array.from({ length: hourCount }, (_, index) =>
      new Date(start + index * 3_600_000).toISOString(),
    ),
    hours: Array.from({ length: hourCount }, (_, index) => ({
      b: [
        ['USA', 'NA', 10 + index, 100] as GeoBucketRow,
        ['CHN', 'AS', 5, 50] as GeoBucketRow,
      ],
    })),
  };
}

function renderTimeline(
  hourCount = 26,
  hourIndex = hourCount - 1,
  overrides: Partial<Parameters<typeof WindowTimeline>[0]> = {},
) {
  const data = makeData(hourCount);
  const onHourChange = vi.fn();
  const onTogglePlay = vi.fn();
  render(
    <WindowTimeline
      data={data}
      hourIndex={hourIndex}
      metricModel={deriveGeoMetricModel(data, 'n')}
      onHourChange={onHourChange}
      onTogglePlay={onTogglePlay}
      playing={false}
      {...overrides}
    />,
  );
  return { data, onHourChange, onTogglePlay };
}

afterEach(cleanup);

describe('WindowTimeline', () => {
  it('renders one stacked layer per continent with a legend and day gridlines', () => {
    renderTimeline();

    expect(document.querySelector('path[data-continent="NA"]')).toBeInTheDocument();
    expect(document.querySelector('path[data-continent="AS"]')).toBeInTheDocument();
    expect(screen.getByText('N. America')).toBeInTheDocument();
    expect(screen.getByText('Asia')).toBeInTheDocument();
    expect(screen.getByText('Jul 15')).toBeInTheDocument();
    expect(screen.getByText('Jul 16 · 00:00 UTC')).toBeInTheDocument();
    expect(screen.queryByText(/p99/)).not.toBeInTheDocument();
  });

  it('seeks with clicks mapped through the plotted area', () => {
    const { onHourChange } = renderTimeline();
    const slider = screen.getByRole('slider', { name: 'Demand timeline for the loaded window' });
    slider.getBoundingClientRect = () => ({ left: 0, width: 1_000 }) as DOMRect;

    fireEvent.pointerDown(slider, { button: 0, pointerId: 1, clientX: 8 });
    expect(onHourChange).toHaveBeenLastCalledWith(0);
    fireEvent.pointerMove(slider, { pointerId: 1, clientX: 992 });
    expect(onHourChange).toHaveBeenLastCalledWith(25);
    fireEvent.pointerUp(slider, { pointerId: 1, clientX: 992 });
    fireEvent.pointerMove(slider, { pointerId: 1, clientX: 500 });
    expect(onHourChange).toHaveBeenCalledTimes(2);
  });

  it('supports keyboard scrubbing across the whole window', () => {
    const { onHourChange } = renderTimeline(26, 10);
    const slider = screen.getByRole('slider', { name: 'Demand timeline for the loaded window' });

    fireEvent.keyDown(slider, { key: 'ArrowRight' });
    expect(onHourChange).toHaveBeenLastCalledWith(11);
    fireEvent.keyDown(slider, { key: 'ArrowLeft' });
    expect(onHourChange).toHaveBeenLastCalledWith(9);
    fireEvent.keyDown(slider, { key: 'PageDown' });
    expect(onHourChange).toHaveBeenLastCalledWith(0);
    fireEvent.keyDown(slider, { key: 'End' });
    expect(onHourChange).toHaveBeenLastCalledWith(25);
    fireEvent.keyDown(slider, { key: 'Home' });
    expect(onHourChange).toHaveBeenLastCalledWith(0);
  });

  it('disables hour steps at the window edges and toggles playback', () => {
    const { onHourChange, onTogglePlay } = renderTimeline(26, 0);

    expect(screen.getByRole('button', { name: '← 1h' })).toBeDisabled();
    const forward = screen.getByRole('button', { name: '1h →' });
    expect(forward).toBeEnabled();
    fireEvent.click(forward);
    expect(onHourChange).toHaveBeenLastCalledWith(1);

    fireEvent.click(screen.getByRole('button', { name: '▶ Play' }));
    expect(onTogglePlay).toHaveBeenCalledOnce();
  });

  it('moves the cursor marker with the selected hour', () => {
    renderTimeline(26, 0);
    const first = screen.getByTestId('selected-hour-marker').getAttribute('x1');
    cleanup();
    renderTimeline(26, 25);
    const last = screen.getByTestId('selected-hour-marker').getAttribute('x1');

    expect(Number(first)).toBeLessThan(Number(last));
  });
});
