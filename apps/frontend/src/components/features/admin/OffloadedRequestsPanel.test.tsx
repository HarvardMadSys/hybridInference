// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AdminRequestOffloadGroup, AdminRequestOffloadsResponse } from '@/lib/api/admin';
import { OffloadedRequestsPanel } from './OffloadedRequestsPanel';

vi.mock('@/lib/api/admin', () => ({
  getRecentRequestOffloads: vi.fn(),
}));

import { getRecentRequestOffloads } from '@/lib/api/admin';

function makeGroup(overrides: Partial<AdminRequestOffloadGroup> = {}): AdminRequestOffloadGroup {
  return {
    model_id: 'glm-4.6',
    endpoint_id: 'glm-4.6:reserved-api',
    request_count: 9,
    failed_count: 2,
    reasons: { queue_wait: 5, engine_wait: 3, last_resort: 1 },
    model_request_count: 120,
    ...overrides,
  };
}

function makeResponse(
  groups: AdminRequestOffloadGroup[],
  overrides: Partial<AdminRequestOffloadsResponse> = {},
): AdminRequestOffloadsResponse {
  return {
    generated_at: '2026-06-30T12:00:00.000Z',
    days: 1,
    total_offloaded: groups.reduce((sum, group) => sum + group.request_count, 0),
    groups,
    truncated: false,
    ...overrides,
  };
}

function rowCells(endpointId: string): string[] {
  const row = screen.getByText(endpointId).closest('tr');
  if (!row) throw new Error(`no row for endpoint ${endpointId}`);
  return within(row)
    .getAllByRole('cell')
    .map((td) => td.textContent ?? '');
}

function headers(): string[] {
  return within(screen.getByRole('table', { name: 'Offloaded requests' }))
    .getAllByRole('columnheader')
    .map((th) => th.textContent ?? '');
}

const defaultProps = {
  userFilter: '',
  sessionFilter: '',
  modelFilter: '',
  requestType: 'all' as const,
};

describe('OffloadedRequestsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it('counts each route by reason, against the share of its model and its failures', async () => {
    vi.mocked(getRecentRequestOffloads).mockResolvedValue(
      makeResponse([
        makeGroup(),
        makeGroup({
          model_id: 'qwen3-coder',
          endpoint_id: 'qwen3-coder:openrouter-api',
          request_count: 4,
          failed_count: 0,
          reasons: { engine_wait: 4 },
          model_request_count: 4000,
        }),
      ]),
    );

    render(<OffloadedRequestsPanel {...defaultProps} />);

    await screen.findByText('glm-4.6:reserved-api');
    expect(headers()).toEqual([
      'Model',
      'Offload route',
      'Queue wait',
      'Engine wait',
      'Last resort',
      'Offloaded',
      'Share',
      'Failed',
    ]);
    expect(rowCells('glm-4.6:reserved-api')).toEqual([
      'glm-4.6',
      'glm-4.6:reserved-api',
      '5',
      '3',
      '1',
      '9',
      '7.5%',
      '2',
    ]);
    // A reason with no requests, and a route with no failures, read as empty.
    expect(rowCells('qwen3-coder:openrouter-api')).toEqual([
      'qwen3-coder',
      'qwen3-coder:openrouter-api',
      '—',
      '4',
      '—',
      '4',
      '0.1%',
      '—',
    ]);
    expect(screen.getByTestId('offloaded-total')).toHaveTextContent('13 offloaded');
  });

  it('shows a tiny share as under a tenth of a percent rather than zero', async () => {
    vi.mocked(getRecentRequestOffloads).mockResolvedValue(
      makeResponse([
        makeGroup({ request_count: 1, reasons: { queue_wait: 1 }, model_request_count: 5000 }),
      ]),
    );

    render(<OffloadedRequestsPanel {...defaultProps} />);

    await screen.findByText('glm-4.6:reserved-api');
    expect(rowCells('glm-4.6:reserved-api')[6]).toBe('<0.1%');
  });

  it('adds an Other column only for a reason it has no column for', async () => {
    vi.mocked(getRecentRequestOffloads).mockResolvedValue(
      makeResponse([
        makeGroup({ request_count: 11, reasons: { queue_wait: 9, future_reason: 2 } }),
      ]),
    );

    render(<OffloadedRequestsPanel {...defaultProps} />);

    await screen.findByText('glm-4.6:reserved-api');
    expect(headers()).toContain('Other');
    expect(rowCells('glm-4.6:reserved-api').slice(2, 7)).toEqual(['9', '—', '—', '2', '11']);
  });

  it('asks for the past day under the tab filters', async () => {
    vi.mocked(getRecentRequestOffloads).mockResolvedValue(makeResponse([]));

    render(
      <OffloadedRequestsPanel
        userFilter="ada"
        sessionFilter="sess-1"
        modelFilter="glm"
        requestType="chat"
      />,
    );

    await waitFor(() =>
      expect(getRecentRequestOffloads).toHaveBeenCalledWith({
        days: 1,
        userId: 'ada',
        sessionId: 'sess-1',
        modelId: 'glm',
        requestType: 'chat',
        refresh: false,
      }),
    );
  });

  it('reloads on a refresh key change and asks the backend to skip its cache', async () => {
    vi.mocked(getRecentRequestOffloads).mockResolvedValue(makeResponse([]));
    const { rerender } = render(<OffloadedRequestsPanel {...defaultProps} refreshKey={0} />);
    await waitFor(() => expect(getRecentRequestOffloads).toHaveBeenCalledTimes(1));

    rerender(<OffloadedRequestsPanel {...defaultProps} refreshKey={1} />);

    await waitFor(() => expect(getRecentRequestOffloads).toHaveBeenCalledTimes(2));
    expect(vi.mocked(getRecentRequestOffloads).mock.calls[1][0]).toMatchObject({ refresh: true });

    rerender(<OffloadedRequestsPanel {...defaultProps} refreshKey={1} modelFilter="glm" />);

    await waitFor(() => expect(getRecentRequestOffloads).toHaveBeenCalledTimes(3));
    expect(vi.mocked(getRecentRequestOffloads).mock.calls[2][0]).toMatchObject({
      modelId: 'glm',
      refresh: false,
    });
  });

  it('discards a stale response that lands after a newer one', async () => {
    let resolveStale: (value: AdminRequestOffloadsResponse) => void = () => {};
    vi.mocked(getRecentRequestOffloads)
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveStale = resolve;
          }),
      )
      .mockResolvedValueOnce(makeResponse([makeGroup({ endpoint_id: 'glm-4.6:fresh-api' })]));

    const { rerender } = render(<OffloadedRequestsPanel {...defaultProps} />);
    rerender(<OffloadedRequestsPanel {...defaultProps} modelFilter="glm" />);
    await screen.findByText('glm-4.6:fresh-api');

    resolveStale(makeResponse([makeGroup({ endpoint_id: 'glm-4.6:stale-api' })]));

    await waitFor(() => expect(screen.queryByText('glm-4.6:stale-api')).not.toBeInTheDocument());
    expect(screen.getByText('glm-4.6:fresh-api')).toBeInTheDocument();
  });

  it('says when nothing was offloaded', async () => {
    vi.mocked(getRecentRequestOffloads).mockResolvedValue(makeResponse([]));

    render(<OffloadedRequestsPanel {...defaultProps} />);

    expect(
      await screen.findByText('No requests were offloaded in the last 24h.'),
    ).toBeInTheDocument();
    expect(screen.getByTestId('offloaded-total')).toHaveTextContent('0 offloaded');
  });

  it('notes that an outcome filter does not narrow the count', async () => {
    vi.mocked(getRecentRequestOffloads).mockResolvedValue(makeResponse([]));

    render(<OffloadedRequestsPanel {...defaultProps} outcome="errors" />);

    expect(await screen.findByText(/not narrowed by the outcome filter/)).toBeInTheDocument();
  });

  it('reports a load failure inline', async () => {
    vi.mocked(getRecentRequestOffloads).mockRejectedValue(new Error('boom'));

    render(<OffloadedRequestsPanel {...defaultProps} />);

    expect(await screen.findByText('Failed to load offloaded requests: boom')).toBeInTheDocument();
    expect(screen.queryByTestId('offloaded-total')).not.toBeInTheDocument();
  });

  it('says when the route list was capped, with the total still whole', async () => {
    vi.mocked(getRecentRequestOffloads).mockResolvedValue(
      makeResponse([makeGroup()], { total_offloaded: 40, truncated: true }),
    );

    render(<OffloadedRequestsPanel {...defaultProps} />);

    expect(
      await screen.findByText(/Showing the 1 routes with the most offloaded requests/),
    ).toBeInTheDocument();
    expect(screen.getByTestId('offloaded-total')).toHaveTextContent('40 offloaded');
  });
});
