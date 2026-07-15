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
    bucket_cols: ['c', 'cc', 'cont', 'n', 'err', 'users', 'tin', 'tout', 'gs', 'p50', 'p90'],
    flow_cols: ['c', 'p', 'e', 'n'],
    providers: [
      {
        id: 'vllm',
        label: 'Local cluster (vLLM)',
        kind: 'local',
        region: 'us-east',
        cont: 'NA',
        coord: [-71.09, 42.36],
      },
      {
        id: 'deepseek',
        label: 'DeepSeek API',
        kind: 'remote_api',
        region: null,
        cont: null,
        coord: null,
      },
    ],
    hours_index: ['2026-07-15T00:00:00+00:00', '2026-07-15T01:00:00+00:00'],
    hours: [
      { b: [], f: [] },
      {
        b: [
          ['USA', 'US', 'NA', 10, 0, 4, 100, 80, 12, 200, 500],
          ['?', '?', '?', 4, 0, 1, 20, 10, 3, null, null],
        ],
        f: [
          ['USA', 'vllm', 'endpoint-a', 3],
          ['USA', 'vllm', 'endpoint-b', 4],
          ['USA', 'deepseek', 'deepseek', 3],
          ['?', 'deepseek', 'deepseek', 4],
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

  it('renders the globe controls, honesty copy, external rail, and DB-IP attribution', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response());
    render(<GeoGlobe />);

    expect(
      await screen.findByRole('img', { name: /Globe of IP-based request origins/ }),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '▶ Play' })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Metric' })).toHaveValue('n');
    expect(screen.getByText('DeepSeek API')).toBeInTheDocument();
    expect(
      screen.getByText(/Origin = network origin \(IP-based\), not residence/),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'IP Geolocation by DB-IP' })).toHaveAttribute(
      'href',
      'https://db-ip.com',
    );
    expect(screen.getByText(/29% unlocated/)).toBeInTheDocument();
  });

  it('aggregates duplicate endpoint rows before rendering and detailing a route', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response());
    render(<GeoGlobe />);

    await waitFor(() => expect(document.querySelectorAll('.geo-demand-flow')).toHaveLength(1));
    const route = document.querySelector('.geo-demand-flow');
    expect(route).toHaveAttribute(
      'aria-label',
      'United States to Local cluster (vLLM): 7 requests',
    );
    if (!route) throw new Error('Expected an aggregated route');
    fireEvent.click(route);

    expect(screen.getByText('United States → Local cluster (vLLM)')).toBeInTheDocument();
    expect(screen.getByText('7 requests this hour')).toBeInTheDocument();
    expect(screen.getByText(/endpoint-b 4 · endpoint-a 3/)).toBeInTheDocument();
  });

  it('changes the selected hour and metric and toggles playback', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response());
    render(<GeoGlobe />);

    expect(await screen.findByText('2026-07-15 01:00 UTC')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '−24h' }));
    expect(screen.getByText('2026-07-15 00:00 UTC')).toBeInTheDocument();

    fireEvent.change(screen.getByRole('combobox', { name: 'Metric' }), {
      target: { value: 'tout' },
    });
    expect(screen.getByRole('combobox', { name: 'Metric' })).toHaveValue('tout');

    fireEvent.click(screen.getByRole('button', { name: '▶ Play' }));
    expect(screen.getByRole('button', { name: '⏸ Pause' })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(screen.getByRole('button', { name: '⏸ Pause' }));
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
      await screen.findByRole('img', { name: /Globe of IP-based request origins/ }),
    ).toBeInTheDocument();
    await waitFor(() => expect(mockedGetGeoAnalytics).toHaveBeenCalledTimes(2));
  });

  it('shows an empty state for a successful window with no requests', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response({ rows_total: 0 }));
    render(<GeoGlobe />);

    expect(await screen.findByText('No request origins yet')).toBeInTheDocument();
    expect(
      screen.queryByRole('img', { name: /Globe of IP-based request origins/ }),
    ).not.toBeInTheDocument();
  });

  it('starts flow animation disabled when reduced motion is preferred', async () => {
    vi.stubGlobal(
      'matchMedia',
      vi.fn().mockReturnValue({
        matches: true,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
    );
    mockedGetGeoAnalytics.mockResolvedValue(response());
    render(<GeoGlobe />);

    const toggle = await screen.findByRole('checkbox', { name: 'flow animation' });
    await waitFor(() => expect(toggle).not.toBeChecked());
  });
});
