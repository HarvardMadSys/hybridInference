// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { buildTimeSiteConfig } from '@/config/site-config';
import type { ApiKeyListItem } from '@/lib/api/user';
import { useApiKeys, useModels, useUpdateApiKey, useUsageStats } from '@/lib/hooks';
import { ApiKeyDetail } from './ApiKeyDetail';
import { limitsSummary } from './keyLimits';
import { ModelsSection } from './ModelsSection';
import { UsageStats } from './UsageStats';

vi.mock('@/lib/hooks', () => ({
  useApiKeys: vi.fn(),
  useModels: vi.fn(),
  // The key's project, when it has limits, is summarized beside its own.
  useProjects: vi.fn(() => ({ data: [] })),
  useUpdateApiKey: vi.fn(),
  useUsageStats: vi.fn(),
}));
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

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

  it('labels the prices with the currency of the response they came in', () => {
    const usdOnly = {
      id: 'legacy',
      name: 'Legacy',
      context_length: 1000,
      max_output_length: 100,
      pricing: { prompt: '0.15', completion: '0.60' },
    };
    const native = {
      id: 'native',
      name: 'Native',
      context_length: 1000,
      max_output_length: 100,
      currency: 'CNY',
      pricing: { prompt: '0.15', completion: '0.60' },
      prices: { prompt: '1', completion: '4' },
    };
    const renderModels = (data: unknown[]) => {
      vi.mocked(useModels).mockReturnValue({
        data: { data },
        isLoading: false,
        error: null,
      } as unknown as ReturnType<typeof useModels>);
      render(
        <SiteConfigProvider initialConfig={cnyConfig}>
          <ModelsSection />
        </SiteConfigProvider>,
      );
    };
    // A USD-only response stays USD on a CNY console: heading and prices alike.
    renderModels([usdOnly]);
    expect(screen.getByRole('columnheader', { name: 'USD / 1M tokens' })).toBeInTheDocument();
    expect(screen.getByText('$0.15')).toBeInTheDocument();
    expect(screen.queryByText(/¥/)).not.toBeInTheDocument();
    cleanup();
    // Prices in two currencies: the heading names none; each price keeps its own.
    renderModels([usdOnly, native]);
    expect(screen.getByRole('columnheader', { name: 'Price / 1M tokens' })).toBeInTheDocument();
    expect(screen.getByText('$0.15')).toBeInTheDocument();
    expect(screen.getByText('¥1')).toBeInTheDocument();
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

describe('CNY API key budgets', () => {
  const PREFIX = 'hyi-native01';

  // The USD fields would mislead: only the native ones may be shown or compared.
  function cnyKey(overrides: Partial<ApiKeyListItem> = {}): ApiKeyListItem {
    return {
      key_prefix: PREFIX,
      key_masked: 'hyi-nati****',
      name: 'ci',
      created_at: '2026-11-01T12:00:00.000Z',
      last_used_at: null,
      status: 'active',
      currency: 'CNY',
      spent_today_usd: 0.6,
      spent_month_usd: 4,
      spent_today: 4,
      spent_month: 29,
      budget_daily_usd: 0.7443,
      budget_monthly_usd: null,
      budget_daily: 5,
      budget_monthly: null,
      allowed_models: null,
      ...overrides,
    };
  }

  let mutateAsync: ReturnType<typeof vi.fn>;

  function setup(key: ApiKeyListItem): void {
    vi.mocked(useApiKeys).mockReturnValue({
      data: { keys: [key] },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useApiKeys>);
    vi.mocked(useModels).mockReturnValue({
      data: { object: 'list', data: [{ id: 'model-a', name: 'Model A' }] },
      isLoading: false,
    } as unknown as ReturnType<typeof useModels>);
    vi.mocked(useUsageStats).mockReturnValue({
      data: {
        quota: { currency: 'CNY', has_key: true, daily_limit: 100, daily_limit_usd: 14.8854 },
      },
    } as unknown as ReturnType<typeof useUsageStats>);
    mutateAsync = vi.fn().mockResolvedValue({});
    vi.mocked(useUpdateApiKey).mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useUpdateApiKey>);
  }

  function renderDetail(): void {
    render(
      <SiteConfigProvider initialConfig={cnyConfig}>
        <ApiKeyDetail keyPrefix={PREFIX} />
      </SiteConfigProvider>,
    );
  }

  it('shows native spend against the native budget and quota', () => {
    setup(cnyKey());
    renderDetail();
    expect(screen.getByText('¥4.00 of ¥5.00')).toBeInTheDocument();
    expect(screen.getByText('¥29.00')).toBeInTheDocument();
    expect(screen.getByLabelText(/^Daily budget \(CNY\)/)).toHaveValue('5');
    expect(
      screen.getByText("Your account's daily quota of ¥100.00 also applies."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/\$/)).not.toBeInTheDocument();
  });

  it('saves a budget in CNY with its currency, never as USD', async () => {
    setup(cnyKey());
    renderDetail();
    fireEvent.change(screen.getByLabelText(/^Daily budget \(CNY\)/), { target: { value: '50' } });
    fireEvent.change(screen.getByLabelText(/^Monthly budget \(CNY\)/), {
      target: { value: '1000000.5' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save limits' }));
    await waitFor(() =>
      expect(mutateAsync).toHaveBeenCalledWith({
        keyPrefix: PREFIX,
        // Above the USD client bound, which applies to USD budgets only.
        changes: { budget_daily: 50, budget_monthly: 1000000.5, currency: 'CNY' },
      }),
    );
  });

  it('keeps a migrated budget with more than four places while another limit changes', async () => {
    setup(cnyKey({ budget_daily: 0.89465 }));
    renderDetail();
    expect(screen.getByLabelText(/^Daily budget \(CNY\)/)).toHaveValue('0.89465');
    fireEvent.click(screen.getByLabelText('Only the models selected below'));
    fireEvent.click(screen.getByRole('checkbox', { name: /Model A/ }));
    const save = screen.getByRole('button', { name: 'Save limits' });
    expect(save).toBeEnabled();
    fireEvent.click(save);
    await waitFor(() =>
      expect(mutateAsync).toHaveBeenCalledWith({
        keyPrefix: PREFIX,
        // The stored budget is neither refused nor sent again.
        changes: { allowed_models: ['model-a'] },
      }),
    );
  });

  it('clears a native budget left blank', async () => {
    setup(cnyKey());
    renderDetail();
    fireEvent.change(screen.getByLabelText(/^Daily budget \(CNY\)/), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save limits' }));
    await waitFor(() =>
      expect(mutateAsync).toHaveBeenCalledWith({
        keyPrefix: PREFIX,
        changes: { budget_daily: null, currency: 'CNY' },
      }),
    );
  });

  it('asks for an amount in CNY', () => {
    setup(cnyKey());
    renderDetail();
    fireEvent.change(screen.getByLabelText(/^Daily budget \(CNY\)/), { target: { value: '-3' } });
    expect(screen.getByText(/Enter an amount in CNY/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save limits' })).toBeDisabled();
  });

  it('never relabels a USD budget as CNY when the native amount is missing', () => {
    const t = (_slot: string, english: string) => english;
    const summary = limitsSummary(cnyKey({ budget_daily: undefined }), t);
    expect(summary).toBe('—/day');
    expect(limitsSummary(cnyKey(), t)).toBe('¥5.00/day');
    // A USD key keeps its USD budget.
    expect(
      limitsSummary(
        cnyKey({ currency: undefined, budget_daily: undefined, budget_daily_usd: 5 }),
        t,
      ),
    ).toBe('$5.00/day');
  });
});
