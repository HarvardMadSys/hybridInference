// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ProvidersTab } from '../ProvidersTab';

vi.mock('@/lib/api/admin', () => ({
  getProviderQuotas: vi.fn(),
  getRoutableProviders: vi.fn(),
  setProviderDisabled: vi.fn(),
  setProviderQuotaKeyDisabled: vi.fn(),
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

vi.mock('@/components/features/admin/PerformanceTab', () => ({
  PerformanceTab: () => null,
}));
vi.mock('@/app/dashboard/admin/ProviderKeysTab', () => ({
  ProviderKeysTab: () => null,
}));
vi.mock('@/app/dashboard/admin/ProviderOverviewTab', () => ({
  ProviderOverviewTab: () => null,
}));

import {
  getProviderQuotas,
  setProviderDisabled,
  setProviderQuotaKeyDisabled,
} from '@/lib/api/admin';

const ACTIVE_KEY = {
  name: 'zai',
  display_name: 'ZAI #1',
  key_index: 1,
  key_configured: true,
  key_masked: 'sk-zaiaaa...aaaa',
  fetched_at: '2026-08-04T00:00:00Z',
  ok: true,
  error: null,
  usages: [],
  disabled: false,
  key_ref: 'ref-active',
  key_disabled: false,
};

const DISABLED_KEY = {
  ...ACTIVE_KEY,
  display_name: 'ZAI #2',
  key_index: null,
  key_masked: 'sk-zaibbb...bbbb',
  ok: false,
  error: 'key_disabled',
  key_ref: 'ref-disabled',
  key_disabled: true,
};

const COOKIE_ONLY = {
  ...ACTIVE_KEY,
  name: 'minimax',
  display_name: 'MiniMax',
  key_index: null,
  key_masked: 'session=...9876',
  key_ref: null,
  key_disabled: false,
};

async function openQuotas() {
  render(<ProvidersTab />);
  fireEvent.click(screen.getByRole('tab', { name: 'Quotas' }));
  await screen.findByText('ZAI #1');
}

async function clickSwitch(name: string) {
  await act(async () => {
    fireEvent.click(screen.getByRole('switch', { name }));
  });
}

describe('ProvidersTab quotas per-key disable', () => {
  beforeEach(() => {
    vi.mocked(getProviderQuotas).mockResolvedValue({
      generated_at: '2026-08-04T00:00:00Z',
      providers: [ACTIVE_KEY, DISABLED_KEY, COOKIE_ONLY],
    });
    vi.mocked(setProviderQuotaKeyDisabled).mockResolvedValue({
      provider: 'zai',
      key_ref: 'ref-active',
      source: 'db',
      status: 'disabled',
      pools_updated: 1,
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('disables a single key after confirmation and reloads the quotas', async () => {
    await openQuotas();

    await clickSwitch('Disable ZAI #1 key sk-zaiaaa...aaaa');

    expect(window.confirm).toHaveBeenCalled();
    expect(setProviderQuotaKeyDisabled).toHaveBeenCalledWith('zai', 'ref-active', true);
    await waitFor(() => expect(getProviderQuotas).toHaveBeenCalledTimes(2));
  });

  it('leaves the key untouched when the confirmation is declined', async () => {
    vi.mocked(window.confirm).mockReturnValue(false);
    await openQuotas();

    await clickSwitch('Disable ZAI #1 key sk-zaiaaa...aaaa');

    expect(setProviderQuotaKeyDisabled).not.toHaveBeenCalled();
  });

  it('re-enables a disabled key without a confirmation prompt', async () => {
    await openQuotas();

    expect(screen.getByText('Key disabled')).toBeInTheDocument();
    await clickSwitch('Enable ZAI #2 key sk-zaibbb...bbbb');

    expect(window.confirm).not.toHaveBeenCalled();
    expect(setProviderQuotaKeyDisabled).toHaveBeenCalledWith('zai', 'ref-disabled', false);
  });

  it('offers no key toggle for cookie-based credentials', async () => {
    await openQuotas();

    expect(screen.queryByRole('switch', { name: /MiniMax key/ })).not.toBeInTheDocument();
    expect(screen.getByRole('switch', { name: 'Disable MiniMax' })).toBeInTheDocument();
  });

  it('keeps the provider-wide toggle separate from the key toggle', async () => {
    await openQuotas();

    await clickSwitch('Disable ZAI #1');

    expect(setProviderDisabled).toHaveBeenCalledWith('zai', true);
    expect(setProviderQuotaKeyDisabled).not.toHaveBeenCalled();
  });
});
