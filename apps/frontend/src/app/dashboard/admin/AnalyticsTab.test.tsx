// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getAnalytics,
  getGeoAnalytics,
  getGrowthAnalytics,
  listRuntimeSettings,
  type ListSettingsResponse,
  getPerformanceMetrics,
  getProviderTokenUsage,
} from '@/lib/api/admin';
import { AnalyticsTab } from './AnalyticsTab';

vi.mock('recharts', () => ({
  Bar: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  BarChart: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  CartesianGrid: () => null,
  Cell: () => null,
  ComposedChart: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  LabelList: () => null,
  Line: () => null,
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
  getGrowthAnalytics: vi.fn(),
  listRuntimeSettings: vi.fn(),
  getPerformanceMetrics: vi.fn(),
  getProviderTokenUsage: vi.fn(),
}));

const SECTION_KEYS = {
  token: 'analytics_token_usage_enabled',
  users: 'analytics_users_and_requests_enabled',
  performance: 'analytics_performance_metrics_enabled',
  growth: 'analytics_growth_rate_enabled',
} as const;
type Section = keyof typeof SECTION_KEYS;
const SECTION_FETCHES = {
  token: getProviderTokenUsage,
  users: getAnalytics,
  performance: getPerformanceMetrics,
  growth: getGrowthAnalytics,
};

function settingsResponse(
  values: Partial<Record<Section, boolean | string>>,
): ListSettingsResponse {
  return {
    settings: (Object.entries(values) as [Section, boolean | string][]).map(([section, value]) => ({
      key: SECTION_KEYS[section],
      value,
      value_type: 'bool',
      default_value: section !== 'growth',
      description: 'Show this Analytics section.',
      min: null,
      max: null,
    })),
  };
}

const ALL_OFF = { token: false, users: false, performance: false, growth: false };

beforeEach(() => {
  vi.mocked(listRuntimeSettings).mockReset().mockResolvedValue({ settings: [] });
  for (const fetch of Object.values(SECTION_FETCHES)) {
    vi.mocked(fetch)
      .mockReset()
      .mockImplementation(() => new Promise<never>(() => undefined));
  }
  vi.mocked(getGeoAnalytics).mockReset();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('AnalyticsTab overview', () => {
  it('loads the overview without fetching the Request origins payload', async () => {
    render(<AnalyticsTab />);
    expect(await screen.findByRole('button', { name: 'Day' })).toBeInTheDocument();
    await waitFor(() => expect(getAnalytics).toHaveBeenCalledWith('day'));
    expect(getGeoAnalytics).not.toHaveBeenCalled();
  });

  it('waits for settings before requesting any section', async () => {
    let finish!: (value: ListSettingsResponse) => void;
    vi.mocked(listRuntimeSettings).mockReturnValueOnce(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    render(<AnalyticsTab />);
    expect(screen.getByRole('status')).toHaveTextContent('Loading Analytics settings');
    for (const fetch of Object.values(SECTION_FETCHES)) expect(fetch).not.toHaveBeenCalled();
    await act(async () => {
      finish(settingsResponse({ ...ALL_OFF, token: true }));
    });
    expect(getProviderTokenUsage).toHaveBeenCalledWith('24h');
    expect(getAnalytics).not.toHaveBeenCalled();
    expect(getPerformanceMetrics).not.toHaveBeenCalled();
    expect(getGrowthAnalytics).not.toHaveBeenCalled();
  });

  it.each(['token', 'users', 'performance', 'growth'] as const)(
    'can enable %s on its own without requesting the other sections',
    async (enabled) => {
      vi.mocked(listRuntimeSettings).mockResolvedValue(
        settingsResponse({ ...ALL_OFF, [enabled]: true }),
      );
      render(<AnalyticsTab />);
      await waitFor(() => expect(SECTION_FETCHES[enabled]).toHaveBeenCalledTimes(1));
      for (const [section, fetch] of Object.entries(SECTION_FETCHES)) {
        if (section !== enabled) expect(fetch).not.toHaveBeenCalled();
      }
      expect(Boolean(screen.queryByRole('button', { name: 'Day' }))).toBe(
        enabled === 'token' || enabled === 'users',
      );
      expect(Boolean(screen.queryByRole('heading', { name: 'Token usage' }))).toBe(
        enabled === 'token',
      );
      expect(Boolean(screen.queryByRole('heading', { name: 'Users and requests' }))).toBe(
        enabled === 'users',
      );
      expect(Boolean(screen.queryByRole('heading', { name: 'Performance metrics' }))).toBe(
        enabled === 'performance',
      );
      expect(Boolean(screen.queryByText('Growth Rate'))).toBe(enabled === 'growth');
    },
  );

  it('shows all enabled sections independently in their overview order', async () => {
    vi.mocked(listRuntimeSettings).mockResolvedValue(
      settingsResponse({ token: true, users: true, performance: true, growth: true }),
    );
    render(<AnalyticsTab />);
    const growth = await screen.findByText('Growth Rate');
    const sections = [
      screen.getByRole('heading', { name: 'Token usage' }),
      screen.getByRole('heading', { name: 'Users and requests' }),
      screen.getByRole('heading', { name: 'Performance metrics' }),
      growth,
    ];
    for (let i = 1; i < sections.length; i++) {
      expect(
        sections[i - 1].compareDocumentPosition(sections[i]) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    }
    for (const fetch of Object.values(SECTION_FETCHES)) expect(fetch).toHaveBeenCalledTimes(1);
  });

  it.each([false, 'true'])(
    'does not request growth unless the setting is boolean true (%s)',
    async (value) => {
      vi.mocked(listRuntimeSettings).mockResolvedValue(settingsResponse({ growth: value }));
      await act(async () => {
        render(<AnalyticsTab />);
      });
      expect(getGrowthAnalytics).not.toHaveBeenCalled();
      expect(screen.queryByText('Growth Rate')).not.toBeInTheDocument();
    },
  );

  it('uses the existing defaults when an older backend omits the keys', async () => {
    await act(async () => {
      render(<AnalyticsTab />);
    });
    expect(getAnalytics).toHaveBeenCalledWith('day');
    expect(getProviderTokenUsage).toHaveBeenCalledWith('24h');
    expect(getPerformanceMetrics).toHaveBeenCalled();
    expect(getGrowthAnalytics).not.toHaveBeenCalled();
    expect(screen.queryByText('Growth Rate')).not.toBeInTheDocument();
  });

  it('offers Settings when all sections are off and fetches no section data', async () => {
    vi.mocked(listRuntimeSettings).mockResolvedValue(settingsResponse(ALL_OFF));
    render(<AnalyticsTab />);
    expect(
      await screen.findByText('All Analytics overview sections are disabled.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open Settings to enable a section' })).toHaveAttribute(
      'href',
      '/dashboard/admin/settings?tab=common#settings-analytics',
    );
    expect(screen.queryByRole('button', { name: 'Day' })).not.toBeInTheDocument();
    for (const fetch of Object.values(SECTION_FETCHES)) expect(fetch).not.toHaveBeenCalled();
  });

  it('shows a settings error without fetching sections and retries the settings', async () => {
    vi.mocked(listRuntimeSettings).mockRejectedValueOnce(new Error('Settings unavailable'));
    vi.mocked(listRuntimeSettings).mockResolvedValueOnce(
      settingsResponse({ ...ALL_OFF, users: true }),
    );
    render(<AnalyticsTab />);
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not load Analytics settings: Settings unavailable',
    );
    for (const fetch of Object.values(SECTION_FETCHES)) expect(fetch).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(getAnalytics).toHaveBeenCalledWith('day'));
    expect(listRuntimeSettings).toHaveBeenCalledTimes(2);
    expect(getProviderTokenUsage).not.toHaveBeenCalled();
    expect(getPerformanceMetrics).not.toHaveBeenCalled();
    expect(getGrowthAnalytics).not.toHaveBeenCalled();
  });

  it('loads enabled growth with its own range and re-reads settings on return', async () => {
    vi.mocked(listRuntimeSettings).mockResolvedValueOnce(settingsResponse({ growth: true }));
    const view = render(<AnalyticsTab />);
    expect(await screen.findByText('Growth Rate')).toBeInTheDocument();
    await waitFor(() => expect(getGrowthAnalytics).toHaveBeenCalledWith(30, expect.anything()));
    const signal = vi.mocked(getGrowthAnalytics).mock.calls[0][1]?.signal;

    fireEvent.click(screen.getByRole('button', { name: 'Hour' }));
    await waitFor(() => expect(getAnalytics).toHaveBeenLastCalledWith('hour'));
    expect(getGrowthAnalytics).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: '60d' }));
    await waitFor(() => expect(getGrowthAnalytics).toHaveBeenLastCalledWith(60, expect.anything()));
    expect(signal?.aborted).toBe(true);

    const activeSignal = vi.mocked(getGrowthAnalytics).mock.calls[1][1]?.signal;
    view.unmount();
    expect(activeSignal?.aborted).toBe(true);
    for (const fetch of Object.values(SECTION_FETCHES)) vi.mocked(fetch).mockClear();
    vi.mocked(listRuntimeSettings).mockResolvedValueOnce(settingsResponse(ALL_OFF));
    await act(async () => {
      render(<AnalyticsTab />);
    });
    expect(listRuntimeSettings).toHaveBeenCalledTimes(2);
    for (const fetch of Object.values(SECTION_FETCHES)) expect(fetch).not.toHaveBeenCalled();
    expect(screen.queryByText('Growth Rate')).not.toBeInTheDocument();
  });

  it('does not let a departed page enable sections after returning with all sections off', async () => {
    let finish!: (value: ListSettingsResponse) => void;
    vi.mocked(listRuntimeSettings).mockReturnValueOnce(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const view = render(<AnalyticsTab />);
    view.unmount();
    vi.mocked(listRuntimeSettings).mockResolvedValueOnce(settingsResponse(ALL_OFF));
    await act(async () => {
      render(<AnalyticsTab />);
      finish(settingsResponse({ growth: true }));
    });
    for (const fetch of Object.values(SECTION_FETCHES)) expect(fetch).not.toHaveBeenCalled();
  });

  it('shows token usage first, over the period the overview shows', async () => {
    render(<AnalyticsTab />);
    const tokenUsage = await screen.findByRole('heading', { name: 'Token usage' });
    const users = screen.getByRole('heading', { name: 'Users and requests' });
    expect(tokenUsage.compareDocumentPosition(users) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    await waitFor(() => expect(getProviderTokenUsage).toHaveBeenCalledWith('24h'));
    for (const [button, period, range] of [
      ['Hour', 'hour', '1h'],
      ['Week', 'week', '7d'],
      ['Month', 'month', '30d'],
    ] as const) {
      fireEvent.click(screen.getByRole('button', { name: button }));
      await waitFor(() => expect(getAnalytics).toHaveBeenLastCalledWith(period));
      expect(getProviderTokenUsage).toHaveBeenLastCalledWith(range);
    }
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
          prompt_tokens: 115000,
          cache_read_tokens: 35000,
          completion_tokens: 35000,
          users: [
            {
              email: 'alice@example.com',
              user_id: 'u1',
              requests: 120,
              tokens: 90000,
              prompt_tokens: 70000,
              cache_read_tokens: 30000,
              completion_tokens: 20000,
            },
            {
              email: 'bob@example.com',
              user_id: 'u2',
              requests: 80,
              tokens: 60000,
              prompt_tokens: 45000,
              cache_read_tokens: 5000,
              completion_tokens: 15000,
            },
          ],
        },
        {
          model: 'gpt-4o',
          requests: 50,
          tokens: 30000,
          prompt_tokens: 25000,
          cache_read_tokens: 0,
          completion_tokens: 5000,
          users: [
            {
              email: 'alice@example.com',
              user_id: 'u1',
              requests: 50,
              tokens: 30000,
              prompt_tokens: 25000,
              cache_read_tokens: 0,
              completion_tokens: 5000,
            },
          ],
        },
      ],
      generated_at: '2026-01-01T00:00:00Z',
    });

    render(<AnalyticsTab />);

    // Card renders the busiest model's top users by default.
    expect(await screen.findByText('Top Users by Model')).toBeInTheDocument();
    expect(await screen.findByText('alice@example.com')).toBeInTheDocument();
    expect(screen.getByText('bob@example.com')).toBeInTheDocument();

    // Each user row breaks its tokens into prefill, cached prefill and output.
    for (const header of ['Prefill', 'Cached', 'Output', 'Tokens']) {
      expect(screen.getByRole('columnheader', { name: header })).toBeInTheDocument();
    }
    const alice = screen.getByText('alice@example.com').closest('tr')!;
    expect(
      within(alice)
        .getAllByRole('cell')
        .map((c) => c.textContent),
    ).toEqual(['1', 'alice@example.com', '120', '60.0%', '70,000', '30,000', '20,000', '90,000']);
    expect(
      screen.getByText(/115,000 prefill \(35,000 cached\) · 35,000 output/),
    ).toBeInTheDocument();

    // Selector lists every model and defaults to the highest-volume one.
    const select = screen.getByRole('combobox', { name: 'Select model' });
    expect(select).toHaveTextContent('claude-sonnet-4-6');
    fireEvent.click(select);
    expect(screen.getByRole('option', { name: 'gpt-4o' })).toBeInTheDocument();
  });
});
