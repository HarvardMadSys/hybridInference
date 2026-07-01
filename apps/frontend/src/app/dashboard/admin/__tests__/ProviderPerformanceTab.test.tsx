// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { ProviderPerformanceTab } from '../ProviderPerformanceTab';

vi.mock('recharts', () => ({
  CartesianGrid: () => null,
  Legend: () => null,
  Line: ({ name }: { name?: string }) => (name ? <span>{name}</span> : null),
  LineChart: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
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
      rows: provider === '__none__' ? [] : [mockStatsRow],
    })),
    getTtftScatter: vi.fn(async () => ({ models: [] })),
  };
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
  });
});
