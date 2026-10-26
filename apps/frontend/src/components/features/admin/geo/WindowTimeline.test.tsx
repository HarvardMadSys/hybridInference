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
      b: [['USA', 'NA', 10 + index, 100] as GeoBucketRow, ['CHN', 'AS', 5, 50] as GeoBucketRow],
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
  const onReturnToTotal = vi.fn();
  render(
    <WindowTimeline
      data={data}
      metricModel={deriveGeoMetricModel(data, 'n')}
      onHourChange={onHourChange}
      onReturnToTotal={onReturnToTotal}
      onTogglePlay={onTogglePlay}
      playing={false}
      selectedCountry={null}
      selectedCountryName={null}
      selectedHourIndex={hourIndex}
      totalLabel={`${Math.max(1, Math.round(hourCount / 24))}-day total`}
      {...overrides}
    />,
  );
  return { data, onHourChange, onTogglePlay, onReturnToTotal };
}

afterEach(cleanup);

describe('WindowTimeline', () => {
  it('renders the total demand with a title, value ticks and day labels', () => {
    renderTimeline();

    expect(document.querySelector('path[data-series="total"]')).toBeInTheDocument();
    expect(document.querySelector('path[data-series="selected"]')).not.toBeInTheDocument();
    expect(document.querySelector('path[data-continent]')).not.toBeInTheDocument();
    expect(screen.getByText('Requests per hour (UTC)')).toBeInTheDocument();
    expect(screen.getByText('0')).toBeInTheDocument();
    expect(screen.getByText('Jul 15')).toBeInTheDocument();
    expect(screen.getByText('Jul 16 · 00:00 UTC')).toBeInTheDocument();
    expect(screen.queryByText(/p99/)).not.toBeInTheDocument();
  });

  it('draws in real pixels, so its text is never stretched', () => {
    renderTimeline();
    const slider = screen.getByRole('slider', { name: 'Demand timeline for the loaded window' });

    expect(slider).not.toHaveAttribute('preserveAspectRatio');
    expect(slider).toHaveAttribute('viewBox', '0 0 1000 112');
  });

  it('overlays the selected country on the total and names it in the title', () => {
    renderTimeline(26, 25, { selectedCountry: 'CHN', selectedCountryName: 'China' });

    const total = document.querySelector('path[data-series="total"]')!.getAttribute('d');
    const selected = document.querySelector('path[data-series="selected"]')!.getAttribute('d');
    expect(selected).toBeTruthy();
    expect(selected).not.toBe(total);
    expect(screen.getByText('China')).toBeInTheDocument();
  });

  it('shows a focus outline for keyboard focus only, never for a pointer scrub', () => {
    renderTimeline();
    const classes = screen
      .getByRole('slider', { name: 'Demand timeline for the loaded window' })
      .getAttribute('class')
      ?.split(' ');

    expect(classes).toContain('focus:outline-none');
    expect(classes).toContain('focus-visible:outline');
    expect(classes).toContain('focus-visible:outline-sky-300');
  });

  it('seeks with clicks mapped through the plotted area, not the tick margin', () => {
    const { onHourChange } = renderTimeline();
    const slider = screen.getByRole('slider', { name: 'Demand timeline for the loaded window' });
    // 960px of plot, then a 40px margin holding the value ticks.
    slider.getBoundingClientRect = () => ({ left: 0, width: 1_000 }) as DOMRect;

    fireEvent.pointerDown(slider, { button: 0, pointerId: 1, clientX: 0 });
    expect(onHourChange).toHaveBeenLastCalledWith(0);
    fireEvent.pointerMove(slider, { pointerId: 1, clientX: 480 });
    expect(onHourChange).toHaveBeenLastCalledWith(13);
    fireEvent.pointerMove(slider, { pointerId: 1, clientX: 990 });
    expect(onHourChange).toHaveBeenLastCalledWith(25);
    fireEvent.pointerUp(slider, { pointerId: 1, clientX: 990 });
    fireEvent.pointerMove(slider, { pointerId: 1, clientX: 500 });
    expect(onHourChange).toHaveBeenCalledTimes(3);
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

  it('labels the aggregate overview without pretending that the last hour is selected', () => {
    const { onHourChange } = renderTimeline(26, 25, {
      selectedHourIndex: null,
      totalLabel: '14-day total',
    });

    const slider = screen.getByRole('slider', { name: 'Demand timeline for the loaded window' });
    expect(screen.getByText('14-day total')).toBeInTheDocument();
    expect(screen.queryByTestId('selected-hour-marker')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '← 1h' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '1h →' })).toBeDisabled();

    fireEvent.keyDown(slider, { key: 'Home' });
    expect(onHourChange).toHaveBeenLastCalledWith(0);
    expect(screen.queryByRole('button', { name: 'Back to 14-day total' })).not.toBeInTheDocument();
    expect(screen.getByText(/Back to 14-day total/, { selector: 'button' })).toHaveAttribute(
      'tabindex',
      '-1',
    );
  });

  it('returns to the window total from the toolbar while an hour is selected', () => {
    const { onReturnToTotal } = renderTimeline(26, 3, { totalLabel: '14-day total' });

    const back = screen.getByRole('button', { name: 'Back to 14-day total' });
    expect(back).not.toHaveAttribute('aria-hidden', 'true');
    expect(back).not.toHaveAttribute('tabindex');
    fireEvent.click(back);
    expect(onReturnToTotal).toHaveBeenCalledOnce();
  });
});
