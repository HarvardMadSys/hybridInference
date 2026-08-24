// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AdminRequestPerfGroup } from '@/lib/api/admin';
import { RequestPerformancePanel } from './RequestPerformancePanel';

vi.mock('@/lib/api/admin', () => ({
  getRecentRequestsPerformance: vi.fn(),
}));

import { getRecentRequestsPerformance } from '@/lib/api/admin';

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
