// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { buildTimeSiteConfig } from '@/config/site-config';
import { useModels, useUsageStats } from '@/lib/hooks';
import { ModelsSection } from './ModelsSection';
import { UsageStats } from './UsageStats';

vi.mock('@/lib/hooks', () => ({ useModels: vi.fn(), useUsageStats: vi.fn() }));
afterEach(cleanup);

const cnyConfig = { ...buildTimeSiteConfig, accounting: { currency: 'CNY' as const } };

describe('CNY accounting dashboard', () => {
  it('renders the native tariff including cache hits instead of the compatibility USD tariff', () => {
    vi.mocked(useModels).mockReturnValue({
      data: {
        data: [
          {
            id: 'test',
            name: 'Test',
            context_length: 1000,
            max_output_length: 100,
            currency: 'CNY',
            pricing: { prompt: '0.15', completion: '0.60', input_cache_reads: '0.003' },
            prices: { prompt: '1', completion: '4', input_cache_reads: '0.02' },
          },
        ],
      },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useModels>);
    render(
      <SiteConfigProvider initialConfig={cnyConfig}>
        <ModelsSection />
      </SiteConfigProvider>,
    );
    expect(screen.getByRole('columnheader', { name: 'CNY / 1M tokens' })).toBeInTheDocument();
    expect(screen.getByText('¥1')).toBeInTheDocument();
    expect(screen.getByText('¥4')).toBeInTheDocument();
    expect(screen.getByText('¥0.02')).toBeInTheDocument();
    expect(screen.queryByText('$0.15')).not.toBeInTheDocument();
  });

  it('renders actual native usage and remaining quota without converting USD values', () => {
    vi.mocked(useUsageStats).mockReturnValue({
      data: {
        period: 'today',
        currency: 'CNY',
        quota: {
          currency: 'CNY',
          has_key: true,
          remaining_today: 93,
          daily_limit: 100,
          remaining_today_usd: 14,
          daily_limit_usd: 15,
        },
        usage: {
          currency: 'CNY',
          cost: 7,
          cost_usd: 1,
          requests: 1,
          prompt_tokens: 2,
          completion_tokens: 3,
        },
      },
      isLoading: false,
      error: null,
    } as ReturnType<typeof useUsageStats>);
    render(
      <SiteConfigProvider initialConfig={cnyConfig}>
        <UsageStats />
      </SiteConfigProvider>,
    );
    expect(screen.getByText('¥93.00')).toBeInTheDocument();
    expect(screen.getByText('of ¥100.00')).toBeInTheDocument();
    expect(screen.getByText('¥7.00')).toBeInTheDocument();
    expect(screen.queryByText('$1.00')).not.toBeInTheDocument();
  });
});
