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
  ProviderKeysTab: ({
    initialProvider,
    onProviderChange,
  }: {
    initialProvider: string;
    onProviderChange: (provider: string) => void;
  }) => <button onClick={() => onProviderChange('openrouter')}>Keys for {initialProvider}</button>,
}));
vi.mock('@/app/dashboard/admin/ProviderOverviewTab', () => ({
  ProviderOverviewTab: ({ onManageKeys }: { onManageKeys: (provider: string) => void }) => (
    <button onClick={() => onManageKeys('zai')}>Manage keys for zai</button>
  ),
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

async function openQuotas(expectedText = 'ZAI #1') {
  render(<ProvidersTab />);
  fireEvent.click(screen.getByRole('tab', { name: 'Quotas' }));
  await screen.findByText(expectedText);
}

async function clickSwitch(name: string) {
  await act(async () => {
    fireEvent.click(screen.getByRole('switch', { name }));
  });
}

describe('ProvidersTab quotas', () => {
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

  it('opens the requested provider keys and retains selection across tabs', () => {
    render(<ProvidersTab />);
    fireEvent.click(screen.getByRole('button', { name: 'Manage keys for zai' }));
    expect(screen.getByRole('tab', { name: 'Keys' })).toHaveAttribute('aria-selected', 'true');
    fireEvent.click(screen.getByRole('button', { name: 'Keys for zai' }));
    fireEvent.click(screen.getByRole('tab', { name: 'Overview' }));
    fireEvent.click(screen.getByRole('tab', { name: 'Keys' }));
    expect(screen.getByRole('button', { name: 'Keys for openrouter' })).toBeInTheDocument();
  });

  it('explains an empty quota response and links to framework setup documentation', async () => {
    vi.mocked(getProviderQuotas).mockResolvedValueOnce({
      generated_at: '2026-08-04T00:00:00Z',
      providers: [],
    });

    await openQuotas('No quota data to display.');

    expect(screen.getByRole('tab', { name: 'Quotas' })).toHaveAttribute('aria-selected', 'true');
    expect(
      screen.getByText('Quota reporting requires a configured backend extension and data source.'),
    ).toBeInTheDocument();
    const docsLink = screen.getByRole('link', { name: 'Quota reporting documentation' });
    expect(docsLink).toHaveAttribute(
      'href',
      'https://github.com/HarvardMadSys/hybridInference/blob/dev/docs/developer/configuration.md#quota-reporting',
    );
    expect(docsLink).toHaveAttribute('target', '_blank');
    expect(docsLink).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.queryByText('No provider data.')).not.toBeInTheDocument();
    expect(screen.queryByRole('switch')).not.toBeInTheDocument();
  });

  it('renders returned quota usage without the empty guidance', async () => {
    vi.mocked(getProviderQuotas).mockResolvedValueOnce({
      generated_at: '2026-08-04T00:00:00Z',
      providers: [
        {
          ...ACTIVE_KEY,
          name: 'example',
          display_name: 'Example Provider',
          key_masked: '...1234',
          key_ref: null,
          usages: [{ label: 'Requests', used: 25, limit: 100, unit: 'requests', reset_at: null }],
        },
      ],
    });

    await openQuotas('Example Provider');

    expect(screen.getByText('Requests')).toBeInTheDocument();
    expect(screen.getByText(/25 \/ 100 requests/)).toBeInTheDocument();
    expect(screen.getByText('(25%)')).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: 'Disable Example Provider' })).toBeInTheDocument();
    expect(screen.queryByText('No quota data to display.')).not.toBeInTheDocument();
    expect(
      screen.queryByRole('link', { name: 'Quota reporting documentation' }),
    ).not.toBeInTheDocument();
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
