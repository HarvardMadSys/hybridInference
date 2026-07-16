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

describe('AnalyticsTab geography entry', () => {
  beforeEach(() => {
    vi.mocked(getAnalytics).mockImplementation(() => new Promise(() => undefined));
    vi.mocked(getGeoAnalytics).mockReset();
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('links to the geo-temporal globe without fetching its large payload', async () => {
    render(<AnalyticsTab />);

    expect(screen.getByRole('link', { name: /geo-temporal demand globe/i })).toHaveAttribute(
      'href',
      '/dashboard/admin/analytics/geo',
    );
    expect(screen.getByText(/request origins \(IP-based\)/i)).toBeInTheDocument();
    await waitFor(() => expect(getAnalytics).toHaveBeenCalledWith('day'));
    expect(getGeoAnalytics).not.toHaveBeenCalled();
  });
});
