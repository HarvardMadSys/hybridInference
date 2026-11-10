// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ProviderTokenUsageResponse, getProviderTokenUsage } from '@/lib/api/admin';
import { TokenUsageSection } from '../TokenUsageSection';

const { row, usage } = vi.hoisted(() => {
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
  const usage = (range: '24h' | '7d', rows: ReturnType<typeof row>[]) => ({
    range,
    window: { from: '2026-05-07T00:00:00.000Z', to: '2026-05-08T00:00:00.000Z' },
    refreshed_at: '2026-05-08T00:00:00.000Z',
    rows,
    totals: {
      input_tokens: 3000,
      output_tokens: 1500,
      cached_tokens: 300,
      reasoning_tokens: 150,
      cost_usd: 3.75,
      request_count: 30,
    },
  });
  return { row, usage };
});

vi.mock('@/lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/admin')>('@/lib/api/admin');
  return {
    ...actual,
    getProviderTokenUsage: vi.fn(async () =>
      // Two endpoints of the same kind, relabelled per route, plus one
      // provider with no configured display name.
      usage('24h', [
        row('local-a', 'Local box A', 'qwen-local'),
        row('local-b', 'Local box B', 'qwen-local'),
        row('zai', null, 'glm-5.1'),
      ]),
    ),
    getPerformanceMetrics: vi.fn(async () => ({ windows: [] })),
  };
});

describe('TokenUsageSection', () => {
  afterEach(() => {
    cleanup();
  });

  it('gives each relabelled endpoint its own table, named and keyed by label', async () => {
    render(<TokenUsageSection range="24h" />);

    // Same kind, same model, but distinct labels — so two separate tables
    // rather than one merged row.
    expect(await screen.findByText('Local box A · local-a')).toBeInTheDocument();
    expect(screen.getByText('Local box B · local-b')).toBeInTheDocument();
  });

  it('falls back to the raw label when no display name is configured', async () => {
    render(<TokenUsageSection range="24h" />);

    expect(await screen.findByText('zai')).toBeInTheDocument();
  });

  it('keeps the range now selected when an earlier one answers late', async () => {
    let answerDay: (resp: ProviderTokenUsageResponse) => void = () => undefined;
    vi.mocked(getProviderTokenUsage)
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            answerDay = resolve;
          }),
      )
      .mockImplementationOnce(async () => usage('7d', [row('zai', null, 'glm-5.1')]));

    const { rerender } = render(<TokenUsageSection range="24h" />);
    rerender(<TokenUsageSection range="7d" />);
    expect(await screen.findByText('zai')).toBeInTheDocument();

    await act(async () => {
      answerDay(usage('24h', [row('stale', null, 'old-model')]));
    });
    expect(screen.queryByText('stale')).not.toBeInTheDocument();
    expect(screen.getByText('zai')).toBeInTheDocument();
  });
});
