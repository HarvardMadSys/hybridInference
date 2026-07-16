// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { GeoAnalyticsResponse } from '@/lib/api/admin';
import { getGeoAnalytics } from '@/lib/api/admin';
import { GeoGlobe } from './GeoGlobe';

vi.mock('@/lib/api/admin', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api/admin')>();
  return { ...actual, getGeoAnalytics: vi.fn() };
});

vi.mock('topojson-client', () => ({
  feature: () => ({
    type: 'FeatureCollection',
    features: [
      {
        type: 'Feature',
        properties: { id: 'USA', name: 'United States' },
        geometry: { type: 'Point', coordinates: [-98, 39] },
      },
    ],
  }),
}));

const mockedGetGeoAnalytics = vi.mocked(getGeoAnalytics);

const ATLAS = {
  type: 'Topology',
  objects: { features: { type: 'GeometryCollection', geometries: [] } },
  arcs: [],
};

function response(overrides: Partial<GeoAnalyticsResponse['meta']> = {}): GeoAnalyticsResponse {
  return {
    meta: {
      source: 'api_logs',
      generated_at: new Date().toISOString(),
      start: '2026-07-15T00:00:00+00:00',
      hours: 2,
      rows_total: 14,
      rows_with_ip: 12,
      geoip: {
        country: true,
        provider: 'dbip-lite',
        attribution: { label: 'IP Geolocation by DB-IP', url: 'https://db-ip.com' },
      },
      degraded: false,
      degraded_reasons: [],
      unmapped_alpha2: [],
      notes: [],
      ...overrides,
    },
    bucket_cols: ['c', 'cont', 'n', 'tout'],
    hours_index: ['2026-07-15T00:00:00+00:00', '2026-07-15T01:00:00+00:00'],
    hours: [
      { b: [] },
      {
        b: [
          ['USA', 'NA', 10, 80],
          ['?', '?', 4, 10],
        ],
      },
    ],
  };
}

class ResizeObserverMock {
  observe() {}
  disconnect() {}
  unobserve() {}
}

beforeEach(() => {
  mockedGetGeoAnalytics.mockReset();
  vi.stubGlobal('ResizeObserver', ResizeObserverMock);
  vi.stubGlobal(
    'matchMedia',
    vi
      .fn()
      .mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
  );
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify(ATLAS), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    ),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('GeoGlobe', () => {
  it('shows a loading state while the 14-day request is pending', () => {
    mockedGetGeoAnalytics.mockReturnValue(new Promise(() => undefined));
    render(<GeoGlobe />);

    expect(screen.getByRole('status', { name: 'Loading geographic demand' })).toBeInTheDocument();
    expect(mockedGetGeoAnalytics).toHaveBeenCalledWith(
      expect.objectContaining({ days: 14, signal: expect.any(AbortSignal) }),
    );
  });

  it('renders request-origin controls, honesty copy, and DB-IP attribution', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response());
    render(<GeoGlobe />);

    expect(
      await screen.findByRole('group', { name: /Globe of IP-based request origins/ }),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '▶ Play' })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Metric' })).toHaveValue('n');
    expect(screen.getByRole('option', { name: 'Requests' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Output tokens' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /Compute/ })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'OC' })).toBeInTheDocument();
    expect(
      screen.getByText(/Origin = network origin \(IP-based\), not residence/),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'IP Geolocation by DB-IP' })).toHaveAttribute(
      'href',
      'https://db-ip.com',
    );
    expect(screen.getByText(/29% unlocated/)).toBeInTheDocument();
    expect(screen.getByText(/not capacity/i)).toBeInTheDocument();
  });

  it('selects a request origin without rendering provider nodes or serving routes', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response());
    render(<GeoGlobe />);

    fireEvent.click(await screen.findByRole('button', { name: 'United States request origin' }));

    expect(screen.getByText('United States · N. America')).toBeInTheDocument();
    expect(screen.getByText(/10 requests this hour/)).toBeInTheDocument();
    expect(screen.getByText('80 output tokens')).toBeInTheDocument();
    expect(screen.queryByText(/distinct users|p90 TTFT/i)).not.toBeInTheDocument();
    expect(document.querySelector('.geo-demand-flow')).not.toBeInTheDocument();
    expect(screen.queryByText(/provider/i)).not.toBeInTheDocument();
    expect(screen.queryByText('Serving split')).not.toBeInTheDocument();
  });

  it('changes the selected hour and metric and toggles playback', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response());
    render(<GeoGlobe />);

    expect(await screen.findByText('2026-07-15 01:00 UTC')).toBeInTheDocument();
    const staticCountryPath = document.querySelector('g[data-layer="countries"] path');
    expect(staticCountryPath).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '−24h' }));
    expect(screen.getByText('2026-07-15 00:00 UTC')).toBeInTheDocument();
    expect(document.querySelector('g[data-layer="countries"] path')).toBe(staticCountryPath);

    fireEvent.change(screen.getByRole('combobox', { name: 'Metric' }), {
      target: { value: 'tout' },
    });
    expect(screen.getByRole('combobox', { name: 'Metric' })).toHaveValue('tout');

    fireEvent.click(screen.getByRole('button', { name: '▶ Play' }));
    expect(screen.getByRole('button', { name: '⏸ Pause' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('Explore origin demand').closest('section')).toHaveAttribute(
      'aria-live',
      'off',
    );
    fireEvent.click(screen.getByRole('button', { name: '⏸ Pause' }));
    expect(screen.getByText('Explore origin demand').closest('section')).toHaveAttribute(
      'aria-live',
      'polite',
    );
  });

  it('maps timeline clicks through the plotted area instead of the SVG margins', async () => {
    const data = response();
    data.hours_index = Array.from(
      { length: 24 },
      (_, hour) => `2026-07-15T${String(hour).padStart(2, '0')}:00:00+00:00`,
    );
    data.hours = Array.from({ length: 24 }, () => ({ b: [] }));
    mockedGetGeoAnalytics.mockResolvedValue(data);
    render(<GeoGlobe />);

    expect(await screen.findByText('2026-07-15 23:00 UTC')).toBeInTheDocument();
    const timeline = screen.getByRole('slider', {
      name: 'Demand timeline for the selected UTC day',
    });
    timeline.getBoundingClientRect = () => ({ left: 0, width: 1_000 }) as DOMRect;
    fireEvent.click(timeline, { clientX: 952 });

    expect(screen.getByText('2026-07-15 23:00 UTC')).toBeInTheDocument();
  });

  it('shows demo, degraded, and stale states without DB-IP attribution', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(
      response({
        source: 'synthetic-demo',
        generated_at: '2020-01-01T00:00:00Z',
        geoip: { country: false, provider: null, attribution: null },
        degraded: true,
        degraded_reasons: ['country_database_missing'],
      }),
    );
    render(<GeoGlobe />);

    expect(await screen.findByText('SYNTHETIC DEMO')).toBeInTheDocument();
    expect(screen.getByText('STALE')).toBeInTheDocument();
    expect(screen.getByText(/Geography is degraded/)).toBeInTheDocument();
    expect(screen.getByText(/country_database_missing/)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'IP Geolocation by DB-IP' })).not.toBeInTheDocument();
  });

  it('offers a retry after a load error', async () => {
    mockedGetGeoAnalytics
      .mockRejectedValueOnce(new Error('scan unavailable'))
      .mockResolvedValueOnce(response());
    render(<GeoGlobe />);

    expect(await screen.findByRole('alert')).toHaveTextContent('scan unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    expect(
      await screen.findByRole('group', { name: /Globe of IP-based request origins/ }),
    ).toBeInTheDocument();
    await waitFor(() => expect(mockedGetGeoAnalytics).toHaveBeenCalledTimes(2));
  });

  it('shows an empty state for a successful window with no requests', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response({ rows_total: 0 }));
    render(<GeoGlobe />);

    expect(await screen.findByText('No request origins yet')).toBeInTheDocument();
    expect(
      screen.queryByRole('group', { name: /Globe of IP-based request origins/ }),
    ).not.toBeInTheDocument();
  });
});
