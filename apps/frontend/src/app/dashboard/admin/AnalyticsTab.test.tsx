// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getAnalytics, getGeoAnalytics } from '@/lib/api/admin';
import { AnalyticsTab } from './AnalyticsTab';

vi.mock('recharts', () => ({
  Bar: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  BarChart: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  Cell: () => null,
  LabelList: () => null,
  Pie: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  PieChart: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  ResponsiveContainer: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  Tooltip: () => null,
  XAxis: () => null,
  YAxis: () => null,
}));

vi.mock('@/lib/api/admin', () => ({
  getAnalytics: vi.fn(),
  getGeoAnalytics: vi.fn(),
}));

describe('AnalyticsTab request-origins entry', () => {
  beforeEach(() => {
    vi.mocked(getAnalytics).mockImplementation(() => new Promise(() => undefined));
    vi.mocked(getGeoAnalytics).mockReset();
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('links to Request origins without fetching its payload from the overview', async () => {
    render(<AnalyticsTab />);

    const section = screen.getByRole('group', { name: 'Analytics section' });
    expect(section).toHaveTextContent('Overview');
    expect(screen.getByRole('link', { name: 'Request origins' })).toHaveAttribute(
      'href',
      '/dashboard/admin/analytics/geo',
    );
    // The Hour/Day/Week/Month period control scopes the overview only; the
    // Request origins page carries its own range control.
    expect(screen.getByRole('button', { name: 'Day' })).toBeInTheDocument();
    await waitFor(() => expect(getAnalytics).toHaveBeenCalledWith('day'));
    expect(getGeoAnalytics).not.toHaveBeenCalled();
  });
});

describe('AnalyticsTab top users by model', () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('renders the per-model top-users table with a model selector', async () => {
    vi.mocked(getAnalytics).mockResolvedValue({
      period: 'day',
      active_users: 5,
      avg_turns: 3,
      avg_user_turns: 1.5,
      sparkline: [],
      top_users: [],
      by_model: [],
      by_provider: [],
      by_model_top_users: [
        {
          model: 'claude-sonnet-4-6',
          requests: 200,
          tokens: 150000,
          users: [
            { email: 'alice@example.com', user_id: 'u1', requests: 120, tokens: 90000 },
            { email: 'bob@example.com', user_id: 'u2', requests: 80, tokens: 60000 },
          ],
        },
        {
          model: 'gpt-4o',
          requests: 50,
          tokens: 30000,
          users: [{ email: 'alice@example.com', user_id: 'u1', requests: 50, tokens: 30000 }],
        },
      ],
      generated_at: '2026-01-01T00:00:00Z',
    });

    render(<AnalyticsTab />);

    // Card renders the busiest model's top users by default.
    expect(await screen.findByText('Top Users by Model')).toBeInTheDocument();
    expect(await screen.findByText('alice@example.com')).toBeInTheDocument();
    expect(screen.getByText('bob@example.com')).toBeInTheDocument();

    // Selector lists every model and defaults to the highest-volume one.
    const select = screen.getByRole('combobox', { name: 'Select model' });
    expect(select).toHaveValue('claude-sonnet-4-6');
    expect(screen.getByRole('option', { name: 'gpt-4o' })).toBeInTheDocument();
  });
});
