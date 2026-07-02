// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { RoutewiseDecisionsPanel, fillBucketGaps } from './RoutewiseDecisionsPanel';

// Render recharts primitives as simple elements so DOM assertions stay robust to
// SVG internals: <Bar> surfaces its `name` as text (legend labels).
vi.mock('recharts', () => ({
  Bar: ({ name }: { name?: string }) => (name ? <span>{name}</span> : null),
  BarChart: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  CartesianGrid: () => null,
  Legend: () => null,
  ResponsiveContainer: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  Tooltip: () => null,
  XAxis: () => null,
  YAxis: () => null,
}));

vi.mock('@/lib/api/admin', () => ({
  getRoutewiseDecisions: vi.fn(),
}));

import { getRoutewiseDecisions } from '@/lib/api/admin';
import type { RoutewiseDecisionsResponse } from '@/lib/api/admin';

const emptyDecisions: RoutewiseDecisionsResponse = {
  model_id: 'minimax-fast',
  range: '24h',
  bucket_seconds: 3600,
  total_requests: 0,
  unattributed_requests: 0,
  lp_status_counts: {},
  selection_share: [],
  hedge_summary: {
    hedged: 0,
    hedge_rate: 0,
    backup_won: 0,
    backup_win_rate: 0,
    median_hedge_delay_ms: null,
  },
  buckets: [],
};

const decisions: RoutewiseDecisionsResponse = {
  model_id: 'minimax-fast',
  range: '24h',
  bucket_seconds: 3600,
  total_requests: 20,
  unattributed_requests: 2,
  lp_status_counts: { optimal: 20 },
  selection_share: [
    { endpoint: 'minimax-fast:openrouter[wandb]-api', provider_type: 'on_demand', count: 10 },
    {
      endpoint: 'minimax-fast:openrouter[minimax/highspeed]-api',
      provider_type: 'concurrency',
      count: 10,
    },
  ],
  // hedge_rate 4/20 = 20.0%; backup_win_rate 2/4 = 50.0%; median 975 ms.
  hedge_summary: {
    hedged: 4,
    hedge_rate: 0.2,
    backup_won: 2,
    backup_win_rate: 0.5,
    median_hedge_delay_ms: 975,
  },
  buckets: [
    {
      bucket_start: '2026-07-01T13:00:00+00:00',
      counts: {
        'minimax-fast:openrouter[wandb]-api': 6,
        'minimax-fast:openrouter[minimax/highspeed]-api': 4,
      },
      hedge: { not_hedged: 8, hedged_primary_won: 1, hedged_backup_won: 1 },
    },
    {
      bucket_start: '2026-07-01T14:00:00+00:00',
      counts: {
        'minimax-fast:openrouter[wandb]-api': 4,
        'minimax-fast:openrouter[minimax/highspeed]-api': 6,
      },
      hedge: { not_hedged: 8, hedged_primary_won: 1, hedged_backup_won: 1 },
    },
  ],
};

describe('RoutewiseDecisionsPanel', () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getRoutewiseDecisions).mockResolvedValue(decisions);
  });

  it('renders the panel header', async () => {
    render(<RoutewiseDecisionsPanel modelId="minimax-fast" />);
    expect(
      await screen.findByRole('heading', { level: 2, name: 'RouteWise decisions' }),
    ).toBeInTheDocument();
  });

  it('renders the distribution short endpoint labels and share summary', async () => {
    render(<RoutewiseDecisionsPanel modelId="minimax-fast" />);

    // Bar legend names use short labels (prefix + trailing -api stripped).
    expect((await screen.findAllByText('openrouter[wandb]')).length).toBeGreaterThan(0);
    expect(screen.getAllByText('openrouter[minimax/highspeed]').length).toBeGreaterThan(0);

    // One-line share summary from selection_share.
    expect(screen.getByTestId('selection-share-summary')).toHaveTextContent(
      'on_demand 50% · concurrency 50%',
    );
    expect(screen.queryByText('2 unattributed')).not.toBeInTheDocument();
  });

  it('renders hedging KPIs and stacked legend labels from the response', async () => {
    render(<RoutewiseDecisionsPanel modelId="minimax-fast" />);

    // KPI chips carry the percentages computed from hedge_summary.
    const kpis = await screen.findByTestId('hedge-kpis');
    expect(kpis).toHaveTextContent('hedge rate 20.0%');
    expect(kpis).toHaveTextContent('backup win rate 50.0%');
    expect(kpis).not.toHaveTextContent('median hedge delay');

    // Stacked hedge bar legend labels (recharts Bar names).
    expect(await screen.findByText('not hedged')).toBeInTheDocument();
    expect(screen.getByText('hedged · primary won')).toBeInTheDocument();
    expect(screen.getByText('hedged · backup won')).toBeInTheDocument();
  });

  it('renders hedge KPIs gracefully when nothing hedged', async () => {
    vi.mocked(getRoutewiseDecisions).mockResolvedValue(emptyDecisions);

    render(<RoutewiseDecisionsPanel modelId="minimax-fast" />);

    const kpis = await screen.findByTestId('hedge-kpis');
    // hedged == 0 -> win rate dash.
    expect(kpis).toHaveTextContent('hedge rate 0.0%');
    expect(kpis).toHaveTextContent('backup win rate —');
    expect(screen.getByText('No hedging activity in this window.')).toBeInTheDocument();
  });

  it('shows empty states when the API returns no rows', async () => {
    vi.mocked(getRoutewiseDecisions).mockResolvedValue(emptyDecisions);

    render(<RoutewiseDecisionsPanel modelId="minimax-fast" />);

    expect(await screen.findByText('No RouteWise decisions in this window.')).toBeInTheDocument();
  });

  it('zero-fills bar data across the whole window so one busy bucket cannot span the chart', async () => {
    render(<RoutewiseDecisionsPanel modelId="minimax-fast" />);

    // The two server buckets still render (legend names come from the Bars),
    // and the empty-state message stays absent even though most generated
    // buckets are zero.
    expect((await screen.findAllByText('openrouter[wandb]')).length).toBeGreaterThan(0);
    expect(screen.queryByText('No RouteWise decisions in this window.')).not.toBeInTheDocument();
    expect(screen.queryByText('No hedging activity in this window.')).not.toBeInTheDocument();
  });

  it('refetches with the new range when the range selector changes', async () => {
    render(<RoutewiseDecisionsPanel modelId="minimax-fast" />);

    await screen.findByRole('heading', { level: 2, name: 'RouteWise decisions' });
    await waitFor(() => {
      expect(getRoutewiseDecisions).toHaveBeenCalledWith('minimax-fast', '24h');
    });

    fireEvent.click(screen.getByRole('button', { name: '7d' }));

    await waitFor(() => {
      expect(getRoutewiseDecisions).toHaveBeenCalledWith('minimax-fast', '7d');
    });
  });
});

describe('fillBucketGaps', () => {
  const emptyHedge = { not_hedged: 0, hedged_primary_won: 0, hedged_backup_won: 0 };

  it('generates the epoch-aligned series over the window, merging server buckets', () => {
    const nowMs = Date.parse('2026-07-01T15:30:00Z');
    const serverBucket = {
      bucket_start: '2026-07-01T13:00:00+00:00',
      counts: { 'minimax-fast:openrouter[wandb]-api': 6 },
      hedge: { not_hedged: 4, hedged_primary_won: 1, hedged_backup_won: 1 },
    };

    const filled = fillBucketGaps([serverBucket], 3600, 24 * 3600, nowMs);

    // floor((now - 24h) / 3600) * 3600 = 2026-06-30T15:00Z, then hourly
    // through the bucket containing now (2026-07-01T15:00Z): 25 buckets.
    expect(filled).toHaveLength(25);
    expect(filled[0].bucket_start).toBe('2026-06-30T15:00:00.000Z');
    expect(filled[filled.length - 1].bucket_start).toBe('2026-07-01T15:00:00.000Z');

    // The server bucket lands on its aligned slot; every other slot is zero.
    expect(filled[22]).toBe(serverBucket);
    for (const [index, bucket] of filled.entries()) {
      if (index === 22) continue;
      expect(bucket.counts).toEqual({});
      expect(bucket.hedge).toEqual(emptyHedge);
    }
  });

  it('returns the input unchanged when bucket_seconds is not positive', () => {
    const buckets = [{ bucket_start: '2026-07-01T13:00:00+00:00', counts: {}, hedge: emptyHedge }];
    expect(fillBucketGaps(buckets, 0, 24 * 3600, Date.now())).toBe(buckets);
  });
});
