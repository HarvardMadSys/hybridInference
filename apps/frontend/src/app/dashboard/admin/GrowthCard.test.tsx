// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AdminGrowthResponse, getGrowthAnalytics } from '@/lib/api/admin';
import GrowthCard from './GrowthCard';

// Surface the data each chart actually receives, so the derived series (moving
// average, cumulative) can be asserted rather than merely rendered.
vi.mock('recharts', () => ({
  Bar: () => null,
  CartesianGrid: () => null,
  Cell: () => null,
  ComposedChart: ({ data }: { data?: unknown[] }) => (
    <div data-testid="chart" data-series={JSON.stringify(data)} />
  ),
  Line: () => null,
  ResponsiveContainer: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  Tooltip: () => null,
  XAxis: () => null,
  YAxis: () => null,
}));

vi.mock('@/lib/api/admin', () => ({
  getGrowthAnalytics: vi.fn(),
}));

const RESPONSE: AdminGrowthResponse = {
  days: 30,
  points: [
    {
      day: '2026-08-06T00:00:00Z',
      active_users: 10,
      new_users: 10,
      tokens: 1000,
      requests: 30,
      partial: false,
    },
    {
      day: '2026-08-07T00:00:00Z',
      active_users: 20,
      new_users: 5,
      tokens: 2000,
      requests: 60,
      partial: false,
    },
    {
      day: '2026-08-08T00:00:00Z',
      active_users: 3,
      new_users: 1,
      tokens: 300,
      requests: 9,
      partial: true,
    },
  ],
  users_trend: {
    slope_per_day: 10,
    recent_avg: 20,
    previous_avg: 10,
    change_pct: 1,
    compare_days: 1,
  },
  tokens_trend: {
    slope_per_day: 1000,
    recent_avg: 2000,
    previous_avg: 1000,
    change_pct: 1,
    compare_days: 1,
  },
  generated_at: '2026-08-08T12:00:00Z',
};

function seriesOf(index: number) {
  const charts = screen.getAllByTestId('chart');
  return JSON.parse(charts[index].getAttribute('data-series') ?? '[]');
}

describe('GrowthCard', () => {
  beforeEach(() => {
    vi.mocked(getGrowthAnalytics).mockResolvedValue(RESPONSE);
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('loads the 30-day range by default and shows both slopes', async () => {
    render(<GrowthCard />);

    await waitFor(() => expect(getGrowthAnalytics).toHaveBeenCalledWith(30));
    expect(await screen.findByText('Active users')).toBeInTheDocument();
    expect(screen.getByText('Token consumption')).toBeInTheDocument();

    // Slope headline, one per series.
    expect(screen.getByText('+10')).toBeInTheDocument();
    expect(screen.getByText('+1,000')).toBeInTheDocument();
    expect(screen.getAllByText('users/day')).toHaveLength(1);
    expect(screen.getAllByText('tokens/day')).toHaveLength(1);
  });

  it('accumulates distinct users from new_users, not by summing DAU', async () => {
    render(<GrowthCard />);
    await screen.findByText('Active users');

    const users = seriesOf(0);
    expect(users.map((p: { value: number }) => p.value)).toEqual([10, 20, 3]);
    // Summing DAU would give 10/30/33 and count returning users again each day.
    expect(users.map((p: { cumulative: number }) => p.cumulative)).toEqual([10, 15, 16]);

    const tokens = seriesOf(1);
    expect(tokens.map((p: { cumulative: number }) => p.cumulative)).toEqual([1000, 3000, 3300]);
  });

  it('stops the moving average before the still-filling day', async () => {
    render(<GrowthCard />);
    await screen.findByText('Active users');

    const users = seriesOf(0);
    // Day 3 is partial: a half-counted day would drag the trend line down.
    expect(users.map((p: { ma: number | null }) => p.ma)).toEqual([10, 15, null]);
    expect(users.map((p: { partial: boolean }) => p.partial)).toEqual([false, false, true]);
  });

  it('refetches when the range changes', async () => {
    render(<GrowthCard />);
    await waitFor(() => expect(getGrowthAnalytics).toHaveBeenCalledWith(30));

    fireEvent.click(screen.getByRole('button', { name: '60d' }));

    await waitFor(() => expect(getGrowthAnalytics).toHaveBeenCalledWith(60));
  });

  it('reports growth from a flat baseline as having no percentage', async () => {
    vi.mocked(getGrowthAnalytics).mockResolvedValue({
      ...RESPONSE,
      users_trend: { ...RESPONSE.users_trend, previous_avg: 0, change_pct: null },
      tokens_trend: { ...RESPONSE.tokens_trend, previous_avg: 0, change_pct: null },
    });

    render(<GrowthCard />);

    expect(await screen.findAllByText(/no baseline/)).toHaveLength(2);
  });

  it('surfaces a failed fetch instead of an empty chart', async () => {
    vi.mocked(getGrowthAnalytics).mockRejectedValue(new Error('boom'));

    render(<GrowthCard />);

    expect(await screen.findByText('boom')).toBeInTheDocument();
    expect(screen.queryByTestId('chart')).not.toBeInTheDocument();
  });
});
