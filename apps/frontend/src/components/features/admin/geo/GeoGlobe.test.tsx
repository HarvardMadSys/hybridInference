// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
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
let queryClient: QueryClient;

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

/** The headline number, e.g. `14` for "14 requests". */
async function findTotal(value: string) {
  expect(await screen.findByTestId('origins-total')).toHaveTextContent(new RegExp(`^${value}$`));
}

function expectTotal(value: string) {
  expect(screen.getByTestId('origins-total')).toHaveTextContent(new RegExp(`^${value}$`));
}

/** The lead sentence, e.g. "United States leads with 71% of all requests." */
function expectLead(name: string, share: string, unit = 'requests') {
  expect(
    screen.getByText(`leads with ${share} of all ${unit}.`, { exact: false }),
  ).toHaveTextContent(`${name} leads with ${share} of all ${unit}.`);
}

function renderGeoGlobe() {
  return render(
    <QueryClientProvider client={queryClient}>
      <GeoGlobe />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockedGetGeoAnalytics.mockReset();
  queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });
  vi.stubGlobal('ResizeObserver', ResizeObserverMock);
  // jsdom has no 2D context; the globe's land matrix simply is not drawn.
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
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
  queryClient.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('GeoGlobe', () => {
  it('shows a loading state while the default 14-day request is pending', () => {
    mockedGetGeoAnalytics.mockReturnValue(new Promise(() => undefined));
    renderGeoGlobe();

    expect(screen.getByRole('status', { name: 'Loading request origins' })).toBeInTheDocument();
    expect(mockedGetGeoAnalytics).toHaveBeenCalledWith(
      expect.objectContaining({ days: 14, signal: expect.any(AbortSignal) }),
    );
  });

  it('renders the hero, controls, and footer without internal identifiers', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response());
    renderGeoGlobe();

    await findTotal('14');
    expect(screen.getByText('requests from 1 country')).toBeInTheDocument();
    expect(screen.getByText('Last 14 days · through Jul 15 01:00 UTC')).toBeInTheDocument();
    expectLead('United States', '71%');

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

  it('keeps cached origins visible while a remount refreshes stale data', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response());
    const firstView = renderGeoGlobe();
    await findTotal('14');
    firstView.unmount();

    queryClient.setQueryData(['admin', 'analytics', 'geo', 14], response(), {
      updatedAt: Date.now() - 2 * 60_000,
    });
    mockedGetGeoAnalytics.mockReturnValue(new Promise(() => undefined));
    renderGeoGlobe();

    expectTotal('14');
    expect(
      screen.queryByRole('status', { name: 'Loading request origins' }),
    ).not.toBeInTheDocument();
    await waitFor(() => expect(mockedGetGeoAnalytics).toHaveBeenCalledTimes(2));
  });

  it('pluralizes active countries and switches the hero with the metric', async () => {
    const data = response();
    data.hours[1].b.push(['CAN', 'NA', 3, 30]);
    data.meta.rows_total = 17;
    mockedGetGeoAnalytics.mockResolvedValue(data);
    renderGeoGlobe();

    await findTotal('17');
    expect(screen.getByText('requests from 2 countries')).toBeInTheDocument();
    expectLead('United States', '59%');

    fireEvent.click(screen.getByRole('button', { name: 'Tokens' }));
    expectTotal('120');
    expect(screen.getByText('output tokens from 2 countries')).toBeInTheDocument();
    expectLead('United States', '67%', 'output tokens');
  });

  it('drives selection through the top-origins rail with a globe ring', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response());
    renderGeoGlobe();

    const rail = (await screen.findByText(/Top origins ·/)).closest('aside')!;
    const row = within(rail).getByRole('button', { name: /United States/ });
    fireEvent.click(row);

    expect(row).toHaveAttribute('aria-pressed', 'true');
    const dot = screen.getByRole('button', { name: 'United States request origin' });
    expect(dot).toHaveAttribute('aria-pressed', 'true');
    expect(dot.querySelector('circle[data-layer="selection-ring"]')).not.toHaveAttribute('display');
    // The timeline follows the same selection.
    const timeline = screen
      .getByRole('slider', { name: 'Demand timeline for the loaded window' })
      .closest('section')!;
    expect(timeline.querySelector('path[data-series="selected"]')).toBeInTheDocument();
    expect(within(timeline).getByText('United States')).toBeInTheDocument();

    fireEvent.click(row);
    expect(row).toHaveAttribute('aria-pressed', 'false');
    expect(dot).toHaveAttribute('aria-pressed', 'false');
    expect(timeline.querySelector('path[data-series="selected"]')).not.toBeInTheDocument();
  });

  it('shows the empty-hour copy when scrubbed to an hour without requests', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response());
    renderGeoGlobe();

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

  it('defaults to the range total, drills into an hour, and returns to the total', async () => {
    const data = response();
    data.hours[0].b = [['CAN', 'NA', 3, 30]];
    data.meta.rows_total = 17;
    mockedGetGeoAnalytics.mockResolvedValue(data);
    renderGeoGlobe();

    await findTotal('17');
    expect(screen.getByText('Last 14 days · through Jul 15 01:00 UTC')).toBeInTheDocument();
    expectLead('United States', '59%');
    expect(screen.getByText('Top origins · last 14 days')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Back to 14-day total' })).not.toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'Globe of IP-based request origins' })).toBeVisible();

    // The return control is laid out (but hidden) on the total, so drilling
    // in reveals the same toolbar element instead of inserting a new row.
    const timelineSection = screen
      .getByRole('slider', { name: 'Demand timeline for the loaded window' })
      .closest('section')!;
    const reserved = within(timelineSection).getByText(/Back to 14-day total/, {
      selector: 'button',
    });
    expect(reserved).toHaveAttribute('aria-hidden', 'true');
    expect(reserved).toBeDisabled();
    expect(within(timelineSection).getByText('14-day total')).toBeInTheDocument();

    const timeline = screen.getByRole('slider', { name: 'Demand timeline for the loaded window' });
    fireEvent.keyDown(timeline, { key: 'ArrowLeft' });

    expectTotal('3');
    expect(screen.getByText('Hour of Jul 15 · 00:00 UTC')).toBeInTheDocument();
    expectLead('CAN', '100%');
    expect(screen.getByText('Top origins · 00:00 UTC')).toBeInTheDocument();
    expect(within(timelineSection).getByText('Jul 15 · 00:00 UTC')).toBeInTheDocument();
    expect(
      screen.getByRole('group', { name: 'Globe of IP-based request origins and day and night' }),
    ).toBeVisible();
    const back = within(timelineSection).getByRole('button', { name: 'Back to 14-day total' });
    expect(back).toBe(reserved);
    expect(back).toBeEnabled();
    fireEvent.click(back);

    expectTotal('17');
    expect(screen.getByText('Top origins · last 14 days')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Back to 14-day total' })).not.toBeInTheDocument();
  });

  it('shows each top origin as a share of the whole scope total, unlocated included', async () => {
    const data = response();
    data.hours[0].b = [['CAN', 'NA', 3, 30]];
    data.meta.rows_total = 17;
    mockedGetGeoAnalytics.mockResolvedValue(data);
    renderGeoGlobe();

    const rail = (await screen.findByText('Top origins · last 14 days')).closest('aside')!;
    const usa = within(rail).getByRole('button', {
      name: 'United States: 10 requests, 59% of all requests in selected window',
    });
    expect(within(usa).getByText('59%')).toBeInTheDocument();
    expect(within(usa).getByTestId('origin-share-bar')).toHaveStyle({
      width: `${(10 / 17) * 100}%`,
    });
    expect(
      within(rail).getByRole('button', {
        name: 'CAN: 3 requests, 18% of all requests in selected window',
      }),
    ).toBeInTheDocument();
    expect(
      within(rail).getByText('% of all 17 requests in selected window · 24% unlocated'),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Tokens' }));
    expect(
      within(rail).getByRole('button', {
        name: 'United States: 80 output tokens, 67% of all output tokens in selected window',
      }),
    ).toBeInTheDocument();
    expect(
      within(rail).getByText('% of all 120 output tokens in selected window · 8.3% unlocated'),
    ).toBeInTheDocument();

    fireEvent.keyDown(
      screen.getByRole('slider', { name: 'Demand timeline for the loaded window' }),
      {
        key: 'ArrowLeft',
      },
    );
    expect(
      within(rail).getByRole('button', {
        name: 'CAN: 30 output tokens, 100% of all output tokens this hour',
      }),
    ).toBeInTheDocument();
    expect(within(rail).getByText('% of all 30 output tokens this hour')).toBeInTheDocument();
  });

  it('refetches when the range changes and remembers cached ranges', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response());
    renderGeoGlobe();
    await findTotal('14');

    fireEvent.click(screen.getByRole('button', { name: '7d' }));
    await waitFor(() =>
      expect(mockedGetGeoAnalytics).toHaveBeenLastCalledWith(expect.objectContaining({ days: 7 })),
    );
    await findTotal('14');

    fireEvent.click(screen.getByRole('button', { name: '14d' }));
    await findTotal('14');
    expect(mockedGetGeoAnalytics).toHaveBeenCalledTimes(2);
  });

  it('toggles playback and silences the rail announcements while playing', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response());
    renderGeoGlobe();

    const play = await screen.findByRole('button', { name: '▶ Play' });
    fireEvent.click(play);
    expect(screen.getByRole('button', { name: '⏸ Pause' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Back to 14-day total' })).toBeInTheDocument();
    expect(screen.getByText(/Top origins ·/).closest('aside')).toHaveAttribute('aria-live', 'off');
    fireEvent.click(screen.getByRole('button', { name: '⏸ Pause' }));
    expect(screen.getByText(/Top origins ·/).closest('aside')).toHaveAttribute(
      'aria-live',
      'polite',
    );
  });

  it('opens data details with totals, coverage, and DB-IP attribution', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response());
    renderGeoGlobe();

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
    renderGeoGlobe();

    expect(await screen.findByText('SYNTHETIC DEMO')).toBeInTheDocument();
    expect(screen.getByText('STALE')).toBeInTheDocument();
    expect(screen.getByText(/Location data is degraded/)).toBeInTheDocument();
    expect(screen.queryByText(/country_database_missing/)).not.toBeInTheDocument();
  });

  it('offers a retry after a load error', async () => {
    mockedGetGeoAnalytics
      .mockRejectedValueOnce(new Error('scan unavailable'))
      .mockResolvedValueOnce(response());
    renderGeoGlobe();

    expect(await screen.findByRole('alert')).toHaveTextContent('scan unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    await findTotal('14');
    await waitFor(() => expect(mockedGetGeoAnalytics).toHaveBeenCalledTimes(2));
  });

  it('shows an empty state for a successful window with no requests', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response({ rows_total: 0 }));
    renderGeoGlobe();

    expect(await screen.findByText('No request origins yet')).toBeInTheDocument();
    expect(screen.getByText(/latest 14-day window/)).toBeInTheDocument();
  });

  it('distinguishes unlocated traffic from an empty hour', async () => {
    const data = response();
    data.hours[1].b = [['?', '?', 96, 10]];
    data.meta.rows_total = 96;
    mockedGetGeoAnalytics.mockResolvedValue(data);
    renderGeoGlobe();

    await findTotal('96');
    expect(
      screen.getByText('Origins unknown for all requests in this 14-day window.'),
    ).toBeInTheDocument();
    expect(
      screen.getByText('No located origins in this window — origins are unknown for this traffic.'),
    ).toBeInTheDocument();
    expect(
      screen.queryByText('No requests recorded in this 14-day window.'),
    ).not.toBeInTheDocument();
  });

  it('returns to the range total when a cached range switch swaps the payload', async () => {
    const fourteen = response();
    const seven = response();
    seven.hours_index = ['2026-07-14T05:00:00+00:00'];
    seven.hours = [{ b: [['CAN', 'NA', 3, 30]] }];
    seven.meta = { ...seven.meta, hours: 1, rows_total: 3 };
    mockedGetGeoAnalytics.mockImplementation((options) =>
      Promise.resolve(options?.days === 7 ? seven : fourteen),
    );
    renderGeoGlobe();
    expect(await screen.findByText('Last 14 days · through Jul 15 01:00 UTC')).toBeInTheDocument();

    const timeline = screen.getByRole('slider', { name: 'Demand timeline for the loaded window' });
    fireEvent.keyDown(timeline, { key: 'Home' });
    expect(screen.getByRole('button', { name: 'Back to 14-day total' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '7d' }));
    await findTotal('3');
    expect(screen.getByText('Last 7 days · through Jul 14 05:00 UTC')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Back to 7-day total' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '14d' }));
    expect(await screen.findByText('Last 14 days · through Jul 15 01:00 UTC')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '7d' }));
    expect(await screen.findByText('Last 7 days · through Jul 14 05:00 UTC')).toBeInTheDocument();
    expectLead('CAN', '100%');
    expect(screen.queryByText('Unknown hour')).not.toBeInTheDocument();
    expect(mockedGetGeoAnalytics).toHaveBeenCalledTimes(2);
  });

  it('keeps the range control reachable on an empty window', async () => {
    const empty = response({ rows_total: 0 });
    const thirty = response();
    mockedGetGeoAnalytics.mockImplementation((options) =>
      Promise.resolve(options?.days === 30 ? thirty : empty),
    );
    renderGeoGlobe();

    expect(await screen.findByText('No request origins yet')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '30d' }));
    await findTotal('14');
  });

  it('returns focus to the trigger when data details closes', async () => {
    mockedGetGeoAnalytics.mockResolvedValue(response());
    renderGeoGlobe();

    const trigger = await screen.findByRole('button', { name: 'Data details' });
    trigger.focus();
    fireEvent.click(trigger);
    expect(screen.getByRole('button', { name: 'Close' })).toHaveFocus();

    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });
});
