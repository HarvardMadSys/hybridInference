// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ApiKeyListItem } from '@/lib/api/user';
import { ApiKeyDetail } from './ApiKeyDetail';
import { parseBudget } from './keyLimits';

vi.mock('@/lib/hooks', () => ({
  useApiKeys: vi.fn(),
  useModels: vi.fn(),
  useUpdateApiKey: vi.fn(),
  useUsageStats: vi.fn(),
}));

vi.mock('react-hot-toast', () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

import { useApiKeys, useModels, useUpdateApiKey, useUsageStats } from '@/lib/hooks';

const PREFIX = 'hyi-detail01';

function makeKey(overrides: Partial<ApiKeyListItem> = {}): ApiKeyListItem {
  return {
    key_prefix: PREFIX,
    key_masked: 'hyi-deta****',
    name: 'ci',
    created_at: '2026-11-01T12:00:00.000Z',
    last_used_at: null,
    status: 'active',
    spent_today_usd: 4,
    spent_month_usd: 30,
    budget_daily_usd: null,
    budget_monthly_usd: null,
    allowed_models: null,
    ...overrides,
  };
}

let mutateAsync: ReturnType<typeof vi.fn>;

function setup(key: ApiKeyListItem | null): void {
  vi.mocked(useApiKeys).mockReturnValue({
    data: { keys: key ? [key] : [] },
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof useApiKeys>);
  vi.mocked(useModels).mockReturnValue({
    data: {
      object: 'list',
      data: [
        { id: 'model-a', name: 'Model A' },
        { id: 'model-b', name: 'Model B' },
      ],
    },
    isLoading: false,
  } as unknown as ReturnType<typeof useModels>);
  vi.mocked(useUsageStats).mockReturnValue({
    data: { quota: { has_key: true, daily_limit_usd: 100 } },
  } as unknown as ReturnType<typeof useUsageStats>);
  mutateAsync = vi.fn().mockResolvedValue({});
  vi.mocked(useUpdateApiKey).mockReturnValue({
    mutateAsync,
    isPending: false,
  } as unknown as ReturnType<typeof useUpdateApiKey>);
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('ApiKeyDetail', () => {
  it('shows the key and its spend against its budgets', () => {
    setup(makeKey({ budget_daily_usd: 5 }));
    render(<ApiKeyDetail keyPrefix={PREFIX} />);

    expect(screen.getByRole('heading', { level: 1, name: 'ci' })).toBeInTheDocument();
    expect(screen.getByText('$4.00 of $5.00')).toBeInTheDocument();
    // No monthly budget: the spend alone.
    expect(screen.getByText('$30.00')).toBeInTheDocument();
    expect(screen.getByRole('meter', { name: 'Today' })).toBeInTheDocument();
    expect(screen.queryByRole('meter', { name: 'This month' })).not.toBeInTheDocument();
    expect(
      screen.getByText("Your account's daily quota of $100.00 also applies."),
    ).toBeInTheDocument();
  });

  it('saves only what changed', async () => {
    setup(makeKey({ budget_monthly_usd: 50 }));
    render(<ApiKeyDetail keyPrefix={PREFIX} />);

    const save = screen.getByRole('button', { name: 'Save limits' });
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/^Daily budget \(USD\)/), { target: { value: '5' } });
    fireEvent.click(save);

    await waitFor(() =>
      expect(mutateAsync).toHaveBeenCalledWith({
        keyPrefix: PREFIX,
        changes: { budget_daily_usd: 5 },
      }),
    );
  });

  it('clears a budget left blank', async () => {
    setup(makeKey({ budget_daily_usd: 5 }));
    render(<ApiKeyDetail keyPrefix={PREFIX} />);

    fireEvent.change(screen.getByLabelText(/^Daily budget \(USD\)/), { target: { value: ' ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save limits' }));

    await waitFor(() =>
      expect(mutateAsync).toHaveBeenCalledWith({
        keyPrefix: PREFIX,
        changes: { budget_daily_usd: null },
      }),
    );
  });

  it('refuses an amount that is not one', () => {
    setup(makeKey());
    render(<ApiKeyDetail keyPrefix={PREFIX} />);

    fireEvent.change(screen.getByLabelText(/^Daily budget \(USD\)/), { target: { value: '-3' } });

    expect(screen.getByText(/Enter an amount in USD/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save limits' })).toBeDisabled();
  });

  it('restricts the key to the models picked, and needs at least one', async () => {
    setup(makeKey());
    render(<ApiKeyDetail keyPrefix={PREFIX} />);

    fireEvent.click(screen.getByLabelText('Only the models selected below'));
    expect(screen.getByText(/Pick at least one model/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save limits' })).toBeDisabled();

    fireEvent.click(screen.getByRole('checkbox', { name: /Model B/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Save limits' }));

    await waitFor(() =>
      expect(mutateAsync).toHaveBeenCalledWith({
        keyPrefix: PREFIX,
        changes: { allowed_models: ['model-b'] },
      }),
    );
  });

  it('keeps an allowed model the catalog no longer lists in view', () => {
    setup(makeKey({ allowed_models: ['model-a', 'retired-model'] }));
    render(<ApiKeyDetail keyPrefix={PREFIX} />);

    const retired = screen.getByRole('checkbox', { name: /retired-model/ });
    expect(retired).toBeChecked();
    expect(within(retired.closest('label')!).getByText('no longer available')).toBeInTheDocument();
  });

  it('allows all models again', async () => {
    setup(makeKey({ allowed_models: ['model-a'] }));
    render(<ApiKeyDetail keyPrefix={PREFIX} />);

    fireEvent.click(screen.getByLabelText('All models'));
    fireEvent.click(screen.getByRole('button', { name: 'Save limits' }));

    await waitFor(() =>
      expect(mutateAsync).toHaveBeenCalledWith({
        keyPrefix: PREFIX,
        changes: { allowed_models: null },
      }),
    );
  });

  it('shows a revoked key read-only', () => {
    setup(makeKey({ status: 'revoked', budget_daily_usd: 5 }));
    render(<ApiKeyDetail keyPrefix={PREFIX} />);

    expect(screen.queryByRole('button', { name: 'Save limits' })).not.toBeInTheDocument();
    expect(screen.getByText('$5.00/day')).toBeInTheDocument();
  });

  it('explains a prefix it does not know', () => {
    setup(null);
    render(<ApiKeyDetail keyPrefix={PREFIX} />);

    expect(screen.getByRole('heading', { name: 'API key not found' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /API Keys/ })).toHaveAttribute(
      'href',
      '/dashboard/keys',
    );
  });
});

describe('parseBudget', () => {
  it.each([
    ['', null],
    ['  ', null],
    ['0', 0],
    ['5', 5],
    ['12.5', 12.5],
    ['0.0001', 0.0001],
    ['1000000', 1_000_000],
    ['-1', undefined],
    ['1.00001', undefined],
    ['1000001', undefined],
    ['abc', undefined],
    ['1e3', undefined],
  ])('%j -> %j', (text, expected) => {
    expect(parseBudget(text)).toBe(expected);
  });
});
