// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest';

import { getProviderStats, getTtftScatter } from '@/lib/api/admin';
import { ProviderPerformanceTab } from '../ProviderPerformanceTab';

vi.mock('recharts', () => ({
  CartesianGrid: () => null,
  Legend: () => null,
  Line: ({ name }: { name?: string }) => (name ? <span>{name}</span> : null),
  LineChart: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  ReferenceArea: () => null,
  ResponsiveContainer: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  Scatter: () => null,
  ScatterChart: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  Tooltip: () => null,
  XAxis: () => null,
  YAxis: () => null,
  ZAxis: () => null,
}));

vi.mock('@/lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/admin')>('@/lib/api/admin');
  const mockStatsRow = {
    hour_bucket: '2026-05-07T20:00:00.000Z',
    provider: 'openai',
    model_id: 'gpt-4o-mini',
    request_count: 12,
    error_count: 1,
    total_prompt_tokens: 1200,
    total_completion_tokens: 800,
    ttft_p50_ms: 300,
    ttft_p95_ms: 700,
    ttft_p99_ms: 1100,
    latency_p50_ms: 900,
    latency_p95_ms: 1500,
    latency_p99_ms: 1800,
    throughput_avg_tps: 42,
    throughput_p50_tps: 38,
    throughput_p95_tps: 55,
  };

  return {
    ...actual,
    getProviderStats: vi.fn(async ({ provider }: { provider: string }) => ({
      providers: ['openai'],
      pairs: [{ provider: 'openai', model_id: 'gpt-4o-mini' }],
      window_providers: ['openai'],
      provider_display_names: { openai: 'OpenAI' },
      rows: provider === '__none__' ? [] : [mockStatsRow],
    })),
    getProviderObservability: vi.fn(async () => ({
      provider: 'openai',
      provider_display_name: 'OpenAI',
      window: {
        from: '2026-05-07T00:00:00.000Z',
        to: '2026-05-08T00:00:00.000Z',
      },
      bucket_minutes: 60,
      totals: {
        request_count: 20,
        error_count: 12,
        rate_limited_count: 4,
        timeout_count: 2,
        server_error_count: 2,
        cache_eligible_count: 10,
        cache_hit_count: 4,
        input_tokens: 1200,
        cache_read_tokens: 320,
        cache_write_tokens: 80,
      },
      buckets: [
        {
          start_time: '2026-05-07T20:00:00.000Z',
          request_count: 20,
          error_count: 12,
          cache_eligible_count: 10,
          cache_hit_count: 4,
          cache_read_tokens: 320,
          input_tokens: 1200,
        },
      ],
      error_types: [
        { error_type: 'rate_limited', count: 4, fraction: 4 / 12 },
        { error_type: 'timeout', count: 2, fraction: 2 / 12 },
        { error_type: 'server_error', count: 2, fraction: 2 / 12 },
        { error_type: 'validation', count: 1, fraction: 1 / 12 },
        { error_type: 'auth', count: 1, fraction: 1 / 12 },
        { error_type: 'not_found', count: 1, fraction: 1 / 12 },
        { error_type: 'model_not_found', count: 1, fraction: 1 / 12 },
      ],
    })),
    getTtftScatter: vi.fn(async () => ({
      generated_at: '2026-05-08T00:00:00.000Z',
      models: [],
      refreshing: false,
    })),
  };
});

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  cleanup();
});

describe('ProviderPerformanceTab', () => {
  it('renders compact TTFT and throughput charts in one responsive row without p99', async () => {
    render(<ProviderPerformanceTab />);

    // Wait for the model *section* to load (the model id also appears in the
    // Model filter dropdown, so match the heading specifically).
    await screen.findByRole('heading', { name: 'gpt-4o-mini', level: 3 });

    const compactRow = screen.getByTestId('provider-performance-chart-row');
    expect(compactRow).toHaveClass('grid-cols-1', 'lg:grid-cols-2', 'gap-3');

    const ttftCard = screen.getByTestId('provider-performance-ttft-card');
    expect(ttftCard).toHaveClass('p-3');
    expect(within(ttftCard).getByText('TTFT (ms)')).toBeInTheDocument();
    expect(within(ttftCard).getByText('p50')).toBeInTheDocument();
    expect(within(ttftCard).getByText('p95')).toBeInTheDocument();
    expect(within(ttftCard).queryByText('p99')).not.toBeInTheDocument();

    const throughputCard = screen.getByTestId('provider-performance-throughput-card');
    expect(throughputCard).toHaveClass('p-3');
    expect(within(throughputCard).getByText('Throughput (tokens/sec)')).toBeInTheDocument();

    expect(screen.getByRole('heading', { name: 'Errors' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Cache' })).toBeInTheDocument();
    expect(screen.queryByText('Cache by model')).not.toBeInTheDocument();
    expect(screen.getByText('Eligible reqs')).toBeInTheDocument();
    expect(screen.getByText('Error breakdown')).toBeInTheDocument();
    expect(screen.getByText('rate_limited')).toBeInTheDocument();
    expect(screen.getByText('other')).toBeInTheDocument();
    expect(screen.queryByText('Errors and cache')).not.toBeInTheDocument();
  });

  it('names the selected provider while keeping its raw label visible', async () => {
    render(<ProviderPerformanceTab />);

    // The label is what api_logs and provider_hourly_stats are keyed on, so it
    // stays next to the display name rather than being replaced by it.
    const option = await screen.findByRole('option', { name: 'OpenAI · openai' });
    expect(option).toHaveValue('openai');
    // Also captioned under the Errors section, so a shared screenshot says
    // which endpoint the numbers belong to.
    expect(screen.getAllByText('OpenAI · openai').length).toBeGreaterThan(1);
  });

  it("asks for the selected provider's scatter only, and again on a switch", async () => {
    const stats = vi.mocked(getProviderStats).getMockImplementation()!;
    vi.mocked(getProviderStats).mockImplementation(async (opts) => {
      const resp = await stats(opts);
      return {
        ...resp,
        providers: ['openai', 'zai'],
        pairs: [...resp.pairs, { provider: 'zai', model_id: 'glm-4.6' }],
      };
    });
    onTestFinished(() => {
      vi.mocked(getProviderStats).mockImplementation(stats);
    });
    // Opened by a refresh of the page, which asks the server to recompute.
    render(<ProviderPerformanceTab refreshKey={1} />);

    await waitFor(() =>
      expect(getTtftScatter).toHaveBeenCalledWith({ refresh: true, provider: 'openai' }),
    );
    fireEvent.change(screen.getByRole('combobox', { name: 'Provider' }), {
      target: { value: 'zai' },
    });

    // A switch only reads what the server keeps; it does not recompute again.
    await waitFor(() =>
      expect(getTtftScatter).toHaveBeenLastCalledWith({ refresh: false, provider: 'zai' }),
    );
    expect(getTtftScatter).toHaveBeenCalledTimes(2);
  });

  it('asks the server to recompute the scatter on a refresh of the page', async () => {
    const { rerender } = render(<ProviderPerformanceTab />);
    await waitFor(() =>
      expect(getTtftScatter).toHaveBeenCalledWith({ refresh: false, provider: 'openai' }),
    );

    rerender(<ProviderPerformanceTab refreshKey={1} />);

    await waitFor(() =>
      expect(getTtftScatter).toHaveBeenLastCalledWith({ refresh: true, provider: 'openai' }),
    );
    expect(getTtftScatter).toHaveBeenCalledTimes(2);
  });
});
