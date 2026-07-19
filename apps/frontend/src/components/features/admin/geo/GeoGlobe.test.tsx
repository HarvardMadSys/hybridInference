// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
        attribution: {
          label: 'IP Geolocation by DB-IP',
          url: 'https://db-ip.com/legal/attribution',
        },
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
  // Reduced motion keeps view-request rotations instant in tests.
  vi.stubGlobal(
    'matchMedia',
    vi
      .fn()
      .mockReturnValue({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
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
  it('shows a loading state while the default 14-day request is pending', () => {
    mockedGetGeoAnalytics.mockReturnValue(new Promise(() => undefined));
    render(<GeoGlobe />);

    expect(screen.getByRole('status', { name: 'Loading request origins' })).toBeInTheDocument();
    expect(mockedGetGeoAnalytics).toHaveBeenCalledWith(
      expect.objectContaining({ days: 14, signal: expect.any(AbortSignal) }),
    );
  });

  it('renders the hero, controls, and footer without internal identifiers', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response());
    render(<GeoGlobe />);

    expect(await screen.findByText('14 requests')).toBeInTheDocument();
    expect(screen.getByText('at 01:00 UTC · Jul 15')).toBeInTheDocument();
    expect(screen.getByText('United States 71% · 1 active country')).toBeInTheDocument();

    const metricGroup = screen.getByRole('group', { name: 'Metric' });
    expect(within(metricGroup).getByRole('button', { name: 'Requests' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(within(metricGroup).getByRole('button', { name: 'Tokens' })).toBeInTheDocument();
    const rangeGroup = screen.getByRole('group', { name: 'Range' });
    expect(within(rangeGroup).getByRole('button', { name: '14d' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );

    expect(screen.getByText('Locations are estimated from request IPs.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Data details' })).toBeInTheDocument();
    expect(screen.queryByText(/api_logs/)).not.toBeInTheDocument();
    expect(screen.queryByText(/GeoIP country/)).not.toBeInTheDocument();
    expect(screen.queryByText(/p99/)).not.toBeInTheDocument();
    expect(screen.queryByText(/complementarity/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/local$/)).not.toBeInTheDocument();
  });

  it('pluralizes active countries and switches the hero with the metric', async () => {
    const data = response();
    data.hours[1].b.push(['CAN', 'NA', 3, 30]);
    data.meta.rows_total = 17;
    mockedGetGeoAnalytics.mockResolvedValue(data);
    render(<GeoGlobe />);

    expect(await screen.findByText('17 requests')).toBeInTheDocument();
    expect(screen.getByText('United States 59% · 2 active countries')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Tokens' }));
    expect(screen.getByText('120 output tokens')).toBeInTheDocument();
    expect(screen.getByText('United States 67% · 2 active countries')).toBeInTheDocument();
  });

  it('drives selection through the top-origins rail with a globe ring', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response());
    render(<GeoGlobe />);

    const rail = (await screen.findByText(/Top origins ·/)).closest('aside')!;
    const row = within(rail).getByRole('button', { name: /United States/ });
    fireEvent.click(row);

    expect(row).toHaveAttribute('aria-pressed', 'true');
    const dot = screen.getByRole('button', { name: 'United States request origin' });
    expect(dot).toHaveAttribute('aria-pressed', 'true');
    expect(dot.querySelector('circle[data-layer="selection-ring"]')).not.toHaveAttribute('display');

    fireEvent.click(row);
    expect(row).toHaveAttribute('aria-pressed', 'false');
    expect(dot).toHaveAttribute('aria-pressed', 'false');
  });

  it('shows the empty-hour copy when scrubbed to an hour without requests', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response());
    render(<GeoGlobe />);

    const timeline = await screen.findByRole('slider', {
      name: 'Demand timeline for the loaded window',
    });
    fireEvent.keyDown(timeline, { key: 'ArrowLeft' });

    expect(screen.getByText('No requests recorded in this hour.')).toBeInTheDocument();
    expect(
      screen.getByText('No requests this hour. Press Play or scrub the timeline.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '← 1h' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '1h →' })).toBeEnabled();
  });

  it('refetches when the range changes and remembers cached ranges', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response());
    render(<GeoGlobe />);
    await screen.findByText('14 requests');

    fireEvent.click(screen.getByRole('button', { name: '7d' }));
    await waitFor(() =>
      expect(mockedGetGeoAnalytics).toHaveBeenLastCalledWith(expect.objectContaining({ days: 7 })),
    );
    await screen.findByText('14 requests');

    fireEvent.click(screen.getByRole('button', { name: '14d' }));
    await screen.findByText('14 requests');
    expect(mockedGetGeoAnalytics).toHaveBeenCalledTimes(2);
  });

  it('toggles playback and silences the rail announcements while playing', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response());
    render(<GeoGlobe />);

    const play = await screen.findByRole('button', { name: '▶ Play' });
    fireEvent.click(play);
    expect(screen.getByRole('button', { name: '⏸ Pause' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText(/Top origins ·/).closest('aside')).toHaveAttribute('aria-live', 'off');
    fireEvent.click(screen.getByRole('button', { name: '⏸ Pause' }));
    expect(screen.getByText(/Top origins ·/).closest('aside')).toHaveAttribute(
      'aria-live',
      'polite',
    );
  });

  it('opens data details with totals, coverage, and DB-IP attribution', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response());
    render(<GeoGlobe />);

    fireEvent.click(await screen.findByRole('button', { name: 'Data details' }));
    const dialog = screen.getByRole('dialog', { name: 'Data details' });
    expect(within(dialog).getByText('14')).toBeInTheDocument();
    expect(within(dialog).getByText('90')).toBeInTheDocument();
    expect(within(dialog).getByText(/71% of requests resolved to a country/)).toBeInTheDocument();
    expect(within(dialog).getByRole('link', { name: 'IP Geolocation by DB-IP' })).toHaveAttribute(
      'href',
      'https://db-ip.com/legal/attribution',
    );
    expect(within(dialog).getByText(/99th percentile/)).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('shows demo, degraded, and stale states', async () => {
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
    expect(screen.getByText(/Location data is degraded/)).toBeInTheDocument();
    expect(screen.queryByText(/country_database_missing/)).not.toBeInTheDocument();
  });

  it('offers a retry after a load error', async () => {
    mockedGetGeoAnalytics
      .mockRejectedValueOnce(new Error('scan unavailable'))
      .mockResolvedValueOnce(response());
    render(<GeoGlobe />);

    expect(await screen.findByRole('alert')).toHaveTextContent('scan unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByText('14 requests')).toBeInTheDocument();
    await waitFor(() => expect(mockedGetGeoAnalytics).toHaveBeenCalledTimes(2));
  });

  it('shows an empty state for a successful window with no requests', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response({ rows_total: 0 }));
    render(<GeoGlobe />);

    expect(await screen.findByText('No request origins yet')).toBeInTheDocument();
    expect(screen.getByText(/latest 14-day window/)).toBeInTheDocument();
  });

  it('distinguishes unlocated traffic from an empty hour', async () => {
    const data = response();
    data.hours[1].b = [['?', '?', 96, 10]];
    data.meta.rows_total = 96;
    mockedGetGeoAnalytics.mockResolvedValue(data);
    render(<GeoGlobe />);

    expect(await screen.findByText('96 requests')).toBeInTheDocument();
    expect(screen.getByText('Origins unknown for all requests this hour.')).toBeInTheDocument();
    expect(
      screen.getByText('No located origins this hour — origins are unknown for this traffic.'),
    ).toBeInTheDocument();
    expect(screen.queryByText('No requests recorded in this hour.')).not.toBeInTheDocument();
  });

  it('re-anchors the hour when a cached range switch swaps the payload', async () => {
    const fourteen = response();
    const seven = response();
    seven.hours_index = ['2026-07-14T05:00:00+00:00'];
    seven.hours = [{ b: [['CAN', 'NA', 3, 30]] }];
    seven.meta = { ...seven.meta, hours: 1, rows_total: 3 };
    mockedGetGeoAnalytics.mockImplementation((options) =>
      Promise.resolve(options?.days === 7 ? seven : fourteen),
    );
    render(<GeoGlobe />);
    expect(await screen.findByText('at 01:00 UTC · Jul 15')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '7d' }));
    expect(await screen.findByText('at 05:00 UTC · Jul 14')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '14d' }));
    expect(await screen.findByText('at 01:00 UTC · Jul 15')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '7d' }));
    expect(await screen.findByText('at 05:00 UTC · Jul 14')).toBeInTheDocument();
    expect(screen.getByText('CAN 100% · 1 active country')).toBeInTheDocument();
    expect(screen.queryByText('unknown hour')).not.toBeInTheDocument();
    expect(mockedGetGeoAnalytics).toHaveBeenCalledTimes(2);
  });

  it('keeps the range control reachable on an empty window', async () => {
    const empty = response({ rows_total: 0 });
    const thirty = response();
    mockedGetGeoAnalytics.mockImplementation((options) =>
      Promise.resolve(options?.days === 30 ? thirty : empty),
    );
    render(<GeoGlobe />);

    expect(await screen.findByText('No request origins yet')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '30d' }));
    expect(await screen.findByText('14 requests')).toBeInTheDocument();
  });

  it('returns focus to the trigger when data details closes', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response());
    render(<GeoGlobe />);

    const trigger = await screen.findByRole('button', { name: 'Data details' });
    trigger.focus();
    fireEvent.click(trigger);
    expect(screen.getByRole('button', { name: 'Close' })).toHaveFocus();

    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });
});
