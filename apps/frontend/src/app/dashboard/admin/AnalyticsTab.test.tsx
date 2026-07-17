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
