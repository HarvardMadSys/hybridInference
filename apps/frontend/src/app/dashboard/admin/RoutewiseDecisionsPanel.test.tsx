// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { RoutewiseDecisionsPanel } from './RoutewiseDecisionsPanel';

// Render recharts primitives as simple elements so DOM assertions stay robust to
// SVG internals: <Bar>/<Scatter> surface their `name` as text (legend labels).
vi.mock('recharts', () => ({
  Bar: ({ name }: { name?: string }) => (name ? <span>{name}</span> : null),
  BarChart: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  CartesianGrid: () => null,
  Legend: () => null,
  ReferenceLine: () => null,
  ResponsiveContainer: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  Scatter: ({ name }: { name?: string }) => (name ? <span>{name}</span> : null),
  ScatterChart: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  Tooltip: () => null,
  XAxis: () => null,
  YAxis: () => null,
}));

vi.mock('@/lib/api/admin', () => ({
  getRoutewiseDecisions: vi.fn(),
  listRecentRequests: vi.fn(),
  listRoutewiseSettings: vi.fn(),
}));

import { getRoutewiseDecisions, listRecentRequests, listRoutewiseSettings } from '@/lib/api/admin';
import type { AdminRecentRequestItem, RoutewiseDecisionsResponse } from '@/lib/api/admin';

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

const decisionRow: AdminRecentRequestItem = {
  request_id: 'req-1',
  user_id: null,
  model_id: 'minimax-fast',
  provider: 'openrouter',
  timestamp: '2026-07-01T13:05:00Z',
  routewise: {
    final_endpoint: 'minimax-fast:openrouter[wandb]-api',
    selected_endpoint: 'minimax-fast:openrouter[wandb]-api',
    final_provider_type: 'on_demand',
    lp_status: 'optimal',
    budget_usd: 0.0026,
    hedged: false,
    fallback_attempts: 0,
    lp_weights: {
      'minimax-fast:openrouter[minimax/highspeed]-api': 0.5,
      'minimax-fast:openrouter[wandb]-api': 0.5,
    },
    candidate_costs_usd: {
      'minimax-fast:openrouter[minimax/highspeed]-api': 0.0,
      'minimax-fast:openrouter[wandb]-api': 0.005201,
      'minimax-fast:openrouter[chutes]-api': 0.000385,
    },
    candidate_mean_ttft_sec: {
      'minimax-fast:openrouter[minimax/highspeed]-api': 0.839,
      'minimax-fast:openrouter[wandb]-api': 0.457,
      'minimax-fast:openrouter[chutes]-api': 1.334,
    },
    candidate_provider_types: {
      'minimax-fast:openrouter[minimax/highspeed]-api': 'concurrency',
      'minimax-fast:openrouter[wandb]-api': 'on_demand',
      'minimax-fast:openrouter[chutes]-api': 'quota',
    },
    candidate_mean_ttft_sources: {
      'minimax-fast:openrouter[chutes]-api': 'probe',
    },
    candidate_quota_remaining: {
      'minimax-fast:openrouter[chutes]-api': 5000,
    },
  },
};

const alphaSettings = {
  settings: [
    {
      key: 'routewise_budget_alpha',
      value: 0.5,
      value_type: 'float',
      default_value: 0.5,
      description: 'RouteWise LP cost budget interpolation.',
      min: 0,
      max: 1,
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
    vi.mocked(listRecentRequests).mockResolvedValue({
      requests: [decisionRow],
      total: 1,
      limit: 20,
      offset: 0,
    });
    vi.mocked(listRoutewiseSettings).mockResolvedValue(alphaSettings);
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
      'wandb 50% · minimax/highspeed 50%',
    );
    // Unattributed note when unattributed_requests > 0.
    expect(screen.getByText('2 unattributed')).toBeInTheDocument();
  });

  it('lists a decision and renders scatter tiers plus the budget info line on selection', async () => {
    render(<RoutewiseDecisionsPanel modelId="minimax-fast" />);

    // Explainer row (auto-selects the first decision on load).
    const row = await screen.findByText('optimal');
    fireEvent.click(row);

    // Tier legend labels use paper notation.
    expect(await screen.findByText('on_demand (𝒫_O)')).toBeInTheDocument();
    expect(screen.getByText('quota (𝒫_Q)')).toBeInTheDocument();
    expect(screen.getByText('concurrency (𝒫_C)')).toBeInTheDocument();

    // Info line carries alpha, budget reference, and lp status.
    const infoLine = screen.getByTestId('decision-info-line');
    expect(infoLine).toHaveTextContent('α = 0.5');
    expect(infoLine).toHaveTextContent('budget $0.0026');
    expect(infoLine).toHaveTextContent('optimal');
  });

  it('renders hedging KPIs and stacked legend labels from the response', async () => {
    render(<RoutewiseDecisionsPanel modelId="minimax-fast" />);

    // KPI chips carry the percentages/median computed from hedge_summary.
    const kpis = await screen.findByTestId('hedge-kpis');
    expect(kpis).toHaveTextContent('hedge rate 20.0%');
    expect(kpis).toHaveTextContent('backup win rate 50.0%');
    expect(kpis).toHaveTextContent('median hedge delay 975 ms');

    // Stacked hedge bar legend labels (recharts Bar names).
    expect(await screen.findByText('not hedged')).toBeInTheDocument();
    expect(screen.getByText('hedged · primary won')).toBeInTheDocument();
    expect(screen.getByText('hedged · backup won')).toBeInTheDocument();
  });

  it('renders hedge KPIs gracefully when nothing hedged and the median is null', async () => {
    vi.mocked(getRoutewiseDecisions).mockResolvedValue(emptyDecisions);
    vi.mocked(listRecentRequests).mockResolvedValue({
      requests: [],
      total: 0,
      limit: 20,
      offset: 0,
    });

    render(<RoutewiseDecisionsPanel modelId="minimax-fast" />);

    const kpis = await screen.findByTestId('hedge-kpis');
    // hedged == 0 -> win rate dash; median null -> delay dash.
    expect(kpis).toHaveTextContent('hedge rate 0.0%');
    expect(kpis).toHaveTextContent('backup win rate —');
    expect(kpis).toHaveTextContent('median hedge delay —');
    expect(screen.getByText('No hedging activity in this window.')).toBeInTheDocument();
  });

  it('shows empty states when the API returns no rows', async () => {
    vi.mocked(getRoutewiseDecisions).mockResolvedValue(emptyDecisions);
    vi.mocked(listRecentRequests).mockResolvedValue({
      requests: [],
      total: 0,
      limit: 20,
      offset: 0,
    });

    render(<RoutewiseDecisionsPanel modelId="minimax-fast" />);

    expect(await screen.findByText('No RouteWise decisions in this window.')).toBeInTheDocument();
    expect(
      screen.getByText('No per-request decisions with candidate data in this window.'),
    ).toBeInTheDocument();
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
