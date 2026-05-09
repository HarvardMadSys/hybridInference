// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { UsageStats } from './UsageStats';

vi.mock('@/lib/hooks', () => ({
  useUsageStats: vi.fn(),
}));

import { useUsageStats } from '@/lib/hooks';

const mockedUseUsageStats = useUsageStats as unknown as ReturnType<typeof vi.fn>;

afterEach(() => {
  cleanup();
});

const baseStats = {
  period: 'today' as const,
  quota: {
    has_key: true,
    daily_limit_usd: 10,
    spent_today_usd: 0,
    remaining_today_usd: 10,
    reset_at: new Date('2030-01-01T00:00:00Z').toISOString(),
    reset_timezone: 'UTC',
    contact_email: 'admin@example.com',
  },
  usage: { requests: 0, prompt_tokens: 0, completion_tokens: 0, cost_usd: 0 },
};

describe('UsageStats max_concurrency', () => {
  it('renders the concurrency line when max_concurrency is set', () => {
    mockedUseUsageStats.mockReturnValue({
      data: { ...baseStats, quota: { ...baseStats.quota, max_concurrency: 5 } },
      isLoading: false,
      error: null,
    });
    render(<UsageStats />);
    expect(screen.getByText(/Max concurrent requests:\s*5/)).toBeInTheDocument();
  });

  it('hides the concurrency line when max_concurrency is undefined', () => {
    mockedUseUsageStats.mockReturnValue({
      data: baseStats,
      isLoading: false,
      error: null,
    });
    render(<UsageStats />);
    expect(screen.queryByText(/Max concurrent requests:/)).not.toBeInTheDocument();
  });
});
