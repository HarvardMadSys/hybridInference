// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AdminRequestPerfGroup } from '@/lib/api/admin';
import { RequestPerformancePanel } from './RequestPerformancePanel';
import { buildTrendPoints } from './EndpointTrendCharts';

vi.mock('@/lib/api/admin', () => ({
  getRecentRequestsPerformance: vi.fn(),
  getRecentRequestsPerformanceTrend: vi.fn(),
}));

import { getRecentRequestsPerformance, getRecentRequestsPerformanceTrend } from '@/lib/api/admin';
import type { AdminRequestPerfTrendResponse } from '@/lib/api/admin';

function makeGroup(overrides: Partial<AdminRequestPerfGroup> = {}): AdminRequestPerfGroup {
  return {
    model_id: 'glm-4.6',
    endpoint_id: 'glm-4.6:local-12003',
    request_count: 120,
    ttft_ms: { count: 120, mean: 480.5, p10: 210, p50: 425, p90: 1250 },
    decode_throughput_tps: { count: 90, mean: 42.4, p10: 18.2, p50: 40.1, p90: 63.7 },
    ...overrides,
  };
}

function rowCells(endpointId: string): string[] {
  const cell = screen.getByText(endpointId);
  const row = cell.closest('tr');
  if (!row) throw new Error(`no row for endpoint ${endpointId}`);
  return within(row)
    .getAllByRole('cell')
    .map((td) => td.textContent ?? '');
}

function makeTrend(
  overrides: Partial<AdminRequestPerfTrendResponse> = {},
): AdminRequestPerfTrendResponse {
  const bucket = (hour: number, ttft: number | null, tps: number | null, requests: number) => ({
    start_time: `2026-06-30T${String(hour).padStart(2, '0')}:00:00.000Z`,
    request_count: requests,
    ttft_ms_mean: ttft,
    ttft_ms_p50: ttft,
    ttft_ms_p90: ttft === null ? null : ttft * 2,
    decode_throughput_tps_mean: tps,
    decode_throughput_tps_p50: tps,
    decode_throughput_tps_p90: tps === null ? null : tps * 1.5,
  });
  return {
    generated_at: '2026-06-30T12:00:00.000Z',
    days: 1,
    bucket_minutes: 60,
    series: [
      {
        model_id: 'glm-4.6',
        endpoint_id: 'glm-4.6:local-12003',
        request_count: 30,
        buckets: [
          bucket(9, 200, 40, 10),
          bucket(10, null, null, 0),
          bucket(11, 220, 39, 10),
          bucket(12, 4000, 12, 10),
        ],
      },
    ],
    truncated: false,
    ...overrides,
  };
}

const defaultProps = {
  days: 7,
  userFilter: '',
  modelFilter: '',
  requestType: 'all' as const,
};

describe('RequestPerformancePanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getRecentRequestsPerformance).mockResolvedValue({
      generated_at: '2026-06-30T12:00:00.000Z',
      days: 7,
      groups: [makeGroup()],
      truncated: false,
    });
    vi.mocked(getRecentRequestsPerformanceTrend).mockResolvedValue(makeTrend());
  });

  afterEach(cleanup);

  it('renders mean / median / P10 / P90 for TTFT and decode throughput', async () => {
    render(<RequestPerformancePanel {...defaultProps} />);

    await screen.findByText('glm-4.6:local-12003');
    // model, endpoint, requests, then TTFT mean/median/p10/p90, then decode.
    expect(rowCells('glm-4.6:local-12003')).toEqual([
      'glm-4.6',
      'glm-4.6:local-12003',
      '120',
      '481ms',
      '425ms',
      '210ms',
      '1.3s',
      '42.4',
      '40.1',
      '18.2',
      '63.7',
    ]);
  });

  it('keeps each endpoint of a model as its own row', async () => {
    vi.mocked(getRecentRequestsPerformance).mockResolvedValue({
      generated_at: '2026-06-30T12:00:00.000Z',
      days: 7,
      groups: [
        makeGroup(),
        makeGroup({
          endpoint_id: 'glm-4.6:zai-api',
          request_count: 40,
          ttft_ms: { count: 40, mean: 2100, p10: 1500, p50: 2000, p90: 3100 },
          decode_throughput_tps: { count: 40, mean: 21, p10: 12, p50: 20, p90: 30 },
        }),
      ],
      truncated: false,
    });

    render(<RequestPerformancePanel {...defaultProps} />);

    await screen.findByText('glm-4.6:zai-api');
    expect(screen.getAllByText('glm-4.6')).toHaveLength(2);
    expect(rowCells('glm-4.6:zai-api').slice(2)).toEqual([
      '40',
      '2.1s',
      '2.0s',
      '1.5s',
      '3.1s',
      '21.0',
      '20.0',
      '12.0',
      '30.0',
    ]);
  });

  it('renders a metric with no samples as undefined rather than zeros', async () => {
    vi.mocked(getRecentRequestsPerformance).mockResolvedValue({
      generated_at: '2026-06-30T12:00:00.000Z',
      days: 7,
      groups: [
        makeGroup({
          // Every response decoded faster than the throughput floor: traffic
          // and TTFT are real, throughput is not measurable.
          decode_throughput_tps: { count: 0, mean: null, p10: null, p50: null, p90: null },
        }),
      ],
      truncated: false,
    });

    render(<RequestPerformancePanel {...defaultProps} />);

    await screen.findByText('glm-4.6:local-12003');
    expect(rowCells('glm-4.6:local-12003').slice(-4)).toEqual(['—', '—', '—', '—']);
  });

  it('forwards the tab filters to the API', async () => {
    render(
      <RequestPerformancePanel
        days={30}
        userFilter="ada@example.com"
        modelFilter="glm"
        requestType="chat"
      />,
    );

    await waitFor(() =>
      expect(getRecentRequestsPerformance).toHaveBeenCalledWith({
        days: 30,
        userId: 'ada@example.com',
        modelId: 'glm',
        requestType: 'chat',
        refresh: false,
      }),
    );
  });

  it('reloads on a refresh key change and asks the backend to skip its cache', async () => {
    const { rerender } = render(<RequestPerformancePanel {...defaultProps} refreshKey={0} />);
    await waitFor(() => expect(getRecentRequestsPerformance).toHaveBeenCalledTimes(1));
    // A filter-driven load must ride the cache; only Refresh bypasses it.
    expect(getRecentRequestsPerformance).toHaveBeenLastCalledWith(
      expect.objectContaining({ refresh: false }),
    );

    rerender(<RequestPerformancePanel {...defaultProps} refreshKey={1} />);
    await waitFor(() => expect(getRecentRequestsPerformance).toHaveBeenCalledTimes(2));
    expect(getRecentRequestsPerformance).toHaveBeenLastCalledWith(
      expect.objectContaining({ refresh: true }),
    );

    // A later filter change is not a refresh, even though refreshKey stays at 1.
    rerender(<RequestPerformancePanel {...defaultProps} days={30} refreshKey={1} />);
    await waitFor(() => expect(getRecentRequestsPerformance).toHaveBeenCalledTimes(3));
    expect(getRecentRequestsPerformance).toHaveBeenLastCalledWith(
      expect.objectContaining({ days: 30, refresh: false }),
    );
  });

  it('formats a whole-thousand throughput without a stray decimal', async () => {
    vi.mocked(getRecentRequestsPerformance).mockResolvedValue({
      generated_at: '2026-06-30T12:00:00.000Z',
      days: 7,
      groups: [
        makeGroup({
          // Float arithmetic upstream can land just off a round thousand.
          decode_throughput_tps: {
            count: 10,
            mean: 1000.0000000000001,
            p10: 1500,
            p50: 2000,
            p90: 2500.5,
          },
        }),
      ],
      truncated: false,
    });

    render(<RequestPerformancePanel {...defaultProps} />);

    await screen.findByText('glm-4.6:local-12003');
    expect(rowCells('glm-4.6:local-12003').slice(-4)).toEqual(['1k', '2k', '1.5k', '2.5k']);
  });

  it('notes that "errors only" does not narrow the summary', async () => {
    render(<RequestPerformancePanel {...defaultProps} errorsOnly />);

    expect(await screen.findByText(/not narrowed by/)).toBeInTheDocument();
  });

  it('discards a stale response that lands after a newer one', async () => {
    // The panel refetches as the admin edits filters, so responses can land out
    // of order. Without the sequence guard an older response overwrites the
    // newer rows and the table silently describes the wrong filter.
    function deferred() {
      let resolve!: (value: Awaited<ReturnType<typeof getRecentRequestsPerformance>>) => void;
      const promise = new Promise<Awaited<ReturnType<typeof getRecentRequestsPerformance>>>(
        (r) => (resolve = r),
      );
      return { promise, resolve };
    }
    const first = deferred();
    const second = deferred();
    vi.mocked(getRecentRequestsPerformance)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);

    const { rerender } = render(<RequestPerformancePanel {...defaultProps} days={7} />);
    await waitFor(() => expect(getRecentRequestsPerformance).toHaveBeenCalledTimes(1));

    rerender(<RequestPerformancePanel {...defaultProps} days={30} />);
    await waitFor(() => expect(getRecentRequestsPerformance).toHaveBeenCalledTimes(2));

    // The newer request answers first...
    second.resolve({
      generated_at: '2026-06-30T12:00:00.000Z',
      days: 30,
      groups: [makeGroup({ endpoint_id: 'newer-endpoint' })],
      truncated: false,
    });
    await screen.findByText('newer-endpoint');

    // ...then the older one arrives late and must be ignored.
    first.resolve({
      generated_at: '2026-06-30T12:00:00.000Z',
      days: 7,
      groups: [makeGroup({ endpoint_id: 'stale-endpoint' })],
      truncated: false,
    });
    await waitFor(() => expect(screen.getByText('newer-endpoint')).toBeInTheDocument());
    expect(screen.queryByText('stale-endpoint')).not.toBeInTheDocument();
  });

  it('shows an empty state when nothing matched', async () => {
    vi.mocked(getRecentRequestsPerformance).mockResolvedValue({
      generated_at: '2026-06-30T12:00:00.000Z',
      days: 7,
      groups: [],
      truncated: false,
    });

    render(<RequestPerformancePanel {...defaultProps} />);

    expect(await screen.findByText(/No streaming requests matched/)).toBeInTheDocument();
  });

  it('reports a load failure inline', async () => {
    vi.mocked(getRecentRequestsPerformance).mockRejectedValue(new Error('boom'));

    render(<RequestPerformancePanel {...defaultProps} />);

    expect(await screen.findByText(/Failed to load per-endpoint performance: boom/)).toBeVisible();
  });

  it('says when the group list was capped', async () => {
    vi.mocked(getRecentRequestsPerformance).mockResolvedValue({
      generated_at: '2026-06-30T12:00:00.000Z',
      days: 7,
      groups: [makeGroup()],
      truncated: true,
    });

    render(<RequestPerformancePanel {...defaultProps} />);

    expect(await screen.findByText(/busiest model\/endpoint pairs/)).toBeInTheDocument();
  });
});

describe('RequestPerformancePanel trends', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getRecentRequestsPerformance).mockResolvedValue({
      generated_at: '2026-06-30T12:00:00.000Z',
      days: 1,
      groups: [makeGroup(), makeGroup({ endpoint_id: 'glm-4.6:zai-api' })],
      truncated: false,
    });
    vi.mocked(getRecentRequestsPerformanceTrend).mockResolvedValue(makeTrend());
  });

  afterEach(cleanup);

  async function expandFirstRow() {
    const row = (await screen.findByText('glm-4.6:local-12003')).closest('tr');
    if (!row) throw new Error('no row');
    fireEvent.click(row);
    return row;
  }

  it('does not fetch the trend until a route is opened', async () => {
    render(<RequestPerformancePanel {...defaultProps} days={1} />);
    await screen.findByText('glm-4.6:local-12003');

    expect(getRecentRequestsPerformanceTrend).not.toHaveBeenCalled();

    await expandFirstRow();
    await waitFor(() =>
      expect(getRecentRequestsPerformanceTrend).toHaveBeenCalledWith({
        days: 1,
        userId: undefined,
        modelId: undefined,
        requestType: undefined,
      }),
    );
  });

  it('renders both charts for the opened route', async () => {
    render(<RequestPerformancePanel {...defaultProps} days={1} />);
    await expandFirstRow();

    // One card per metric — never one chart with two y scales, since ms and
    // tok/s share no axis. recharts itself does not lay out under jsdom, so the
    // series mapping is asserted directly in the buildTrendPoints tests below.
    expect(await screen.findByTestId('endpoint-trend-ttft')).toBeInTheDocument();
    expect(screen.getByTestId('endpoint-trend-throughput')).toBeInTheDocument();
    // The summary line reports how much of the window actually had traffic.
    expect(screen.getByText(/3 of 4 hourly buckets/)).toBeInTheDocument();
  });

  it('reuses the one trend response when a second route is opened', async () => {
    render(<RequestPerformancePanel {...defaultProps} days={1} />);
    await expandFirstRow();
    await screen.findByTestId('endpoint-trend-ttft');

    const second = screen.getByText('glm-4.6:zai-api').closest('tr');
    fireEvent.click(second!);

    await waitFor(() => expect(getRecentRequestsPerformanceTrend).toHaveBeenCalledTimes(1));
  });

  it('closes the trend and drops it when the filters change', async () => {
    const { rerender } = render(<RequestPerformancePanel {...defaultProps} days={1} />);
    await expandFirstRow();
    await screen.findByTestId('endpoint-trend-ttft');

    rerender(<RequestPerformancePanel {...defaultProps} days={7} />);

    await waitFor(() =>
      expect(screen.queryByTestId('endpoint-trend-ttft')).not.toBeInTheDocument(),
    );
    // Re-opening asks again, since the cached trend described the old filters.
    await expandFirstRow();
    await waitFor(() => expect(getRecentRequestsPerformanceTrend).toHaveBeenCalledTimes(2));
    expect(getRecentRequestsPerformanceTrend).toHaveBeenLastCalledWith(
      expect.objectContaining({ days: 7 }),
    );
  });

  it('reports a trend failure without breaking the table', async () => {
    vi.mocked(getRecentRequestsPerformanceTrend).mockRejectedValue(new Error('nope'));
    render(<RequestPerformancePanel {...defaultProps} days={1} />);
    await expandFirstRow();

    expect(await screen.findByText(/Failed to load the trend: nope/)).toBeInTheDocument();
    expect(screen.getByText('glm-4.6:local-12003')).toBeInTheDocument();
  });

  it('says so when a route has no measurable samples in the window', async () => {
    vi.mocked(getRecentRequestsPerformanceTrend).mockResolvedValue(
      makeTrend({
        series: [
          {
            model_id: 'glm-4.6',
            endpoint_id: 'glm-4.6:local-12003',
            request_count: 4,
            buckets: [
              {
                start_time: '2026-06-30T09:00:00.000Z',
                request_count: 4,
                ttft_ms_mean: null,
                ttft_ms_p50: null,
                ttft_ms_p90: null,
                decode_throughput_tps_mean: null,
                decode_throughput_tps_p50: null,
                decode_throughput_tps_p90: null,
              },
            ],
          },
        ],
      }),
    );
    render(<RequestPerformancePanel {...defaultProps} days={1} />);
    await expandFirstRow();

    expect(await screen.findAllByText(/No measurable samples/)).toHaveLength(2);
  });
});

describe('buildTrendPoints', () => {
  it('carries a quiet bucket through as a gap rather than a zero', () => {
    const points = buildTrendPoints(makeTrend().series[0]);

    expect(points).toHaveLength(4);
    // The quiet 10:00 bucket keeps its slot on the axis with null values, so the
    // line breaks there instead of sloping across the gap.
    expect(points[1]).toMatchObject({
      ttft_p50: null,
      ttft_p90: null,
      tps_p50: null,
      tps_p90: null,
      requests: 0,
    });
    expect(points.map((p) => p.ttft_p50)).toEqual([200, null, 220, 4000]);
    expect(points.map((p) => p.tps_p50)).toEqual([40, null, 39, 12]);
    expect(points.map((p) => p.ttft_p90)).toEqual([400, null, 440, 8000]);
  });

  it('labels each point with its bucket start time', () => {
    const points = buildTrendPoints(makeTrend().series[0]);
    const expected = new Date('2026-06-30T09:00:00.000Z').toLocaleTimeString([], {
      hour: '2-digit',
      minute: '2-digit',
    });

    expect(points[0].label).toBe(expected);
    expect(new Set(points.map((p) => p.label)).size).toBe(4);
  });
});
