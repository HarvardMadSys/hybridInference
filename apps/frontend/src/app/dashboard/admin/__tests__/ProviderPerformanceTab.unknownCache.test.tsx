// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { getProviderObservability } from '@/lib/api/admin';
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

// Every observability response describes a provider that reports NO cache
// usage at all: cache_eligible_count == 0. The component calls this more than
// once, so the mock is static rather than mockResolvedValueOnce.
vi.mock('@/lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/admin')>('@/lib/api/admin');
  const emptyCacheTotals = {
    request_count: 20,
    error_count: 0,
    rate_limited_count: 0,
    timeout_count: 0,
    server_error_count: 0,
    cache_eligible_count: 0,
    cache_hit_count: 0,
    input_tokens: 1200,
    cache_eligible_input_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
  };
  return {
    ...actual,
    getProviderStats: vi.fn(async () => ({
      providers: ['sglang'],
      pairs: [{ provider: 'sglang', model_id: 'deepseek-v4.1-flash' }],
      window_providers: ['sglang'],
      provider_display_names: { sglang: 'SGLang' },
      rows: [],
    })),
    getProviderObservability: vi.fn(async () => ({
      provider: 'sglang',
      provider_display_name: 'SGLang',
      window: { from: '2026-06-02T00:00:00.000Z', to: '2026-06-03T00:00:00.000Z' },
      bucket_minutes: 60,
      totals: emptyCacheTotals,
      buckets: [],
      error_types: [],
    })),
    getTtftScatter: vi.fn(async () => ({ models: [] })),
  };
});

describe('ProviderPerformanceTab cache-hit rate with no reported cache usage', () => {
  it('renders no data, not 0%, when the provider reports no cache usage', async () => {
    // The three-state contract: a provider whose requests all omit cache usage
    // has cache_eligible_count == 0. Rendering that as 0.00% would claim it
    // demonstrably missed every lookup; it must read as "no data" instead.
    render(<ProviderPerformanceTab />);

    await waitFor(() => expect(getProviderObservability).toHaveBeenCalled());

    await screen.findByRole('heading', { name: 'Cache' });

    // The "Cache hit" KPI must read the no-data dash, not 0.00%. Locate the
    // KPI by its label and inspect the sibling value element, so the (genuinely
    // zero) error/rate-limit KPIs elsewhere on the page are not conflated.
    const cacheHitLabel = screen.getByText('Cache hit');
    const cacheHitValue = cacheHitLabel.nextElementSibling;
    expect(cacheHitValue).not.toBeNull();
    expect(cacheHitValue?.textContent).toBe('—');
    expect(cacheHitValue?.textContent).not.toBe('0.00%');
  });
});
