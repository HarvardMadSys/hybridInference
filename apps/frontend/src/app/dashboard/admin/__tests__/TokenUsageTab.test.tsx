// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { TokenUsageTab } from '../TokenUsageTab';

vi.mock('@/lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/admin')>('@/lib/api/admin');
  const row = (provider: string, provider_display_name: string | null, model_id: string) => ({
    provider,
    provider_display_name,
    model_id,
    input_tokens: 1000,
    output_tokens: 500,
    cached_tokens: 100,
    reasoning_tokens: 50,
    cost_usd: 1.25,
    request_count: 10,
  });

  return {
    ...actual,
    getProviderTokenUsage: vi.fn(async () => ({
      range: '24h',
      window: { from: '2026-05-07T00:00:00.000Z', to: '2026-05-08T00:00:00.000Z' },
      refreshed_at: '2026-05-08T00:00:00.000Z',
      // Two endpoints of the same kind, relabelled per route, plus one
      // provider with no configured display name.
      rows: [
        row('local-a', 'Local box A', 'qwen-local'),
        row('local-b', 'Local box B', 'qwen-local'),
        row('zai', null, 'glm-5.1'),
      ],
      totals: {
        input_tokens: 3000,
        output_tokens: 1500,
        cached_tokens: 300,
        reasoning_tokens: 150,
        cost_usd: 3.75,
        request_count: 30,
      },
    })),
    getPerformanceMetrics: vi.fn(async () => ({ windows: [] })),
  };
});

describe('TokenUsageTab', () => {
  it('gives each relabelled endpoint its own table, named and keyed by label', async () => {
    render(<TokenUsageTab />);

    // Same kind, same model, but distinct labels — so two separate tables
    // rather than one merged row.
    expect(await screen.findByText('Local box A · local-a')).toBeInTheDocument();
    expect(screen.getByText('Local box B · local-b')).toBeInTheDocument();
  });

  it('falls back to the raw label when no display name is configured', async () => {
    render(<TokenUsageTab />);

    expect(await screen.findByText('zai')).toBeInTheDocument();
  });
});
