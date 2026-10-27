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
vi.mock('@/app/dashboard/admin/UpstreamConcurrencyPanel', () => ({
  UpstreamConcurrencyPanel: () => null,
}));

import toast from 'react-hot-toast';
import {
  getProviderQuotas,
  getRoutableProviders,
  setProviderDisabled,
  setProviderQuotaKeyDisabled,
} from '@/lib/api/admin';
import type { ProviderKeyByRefResponse } from '@/lib/api/admin';

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

const CONCURRENCY_USAGE = {
  label: 'Concurrency',
  used: null,
  limit: 10,
  unit: 'slots',
  reset_at: null,
};

const QUOTA_USAGES = [
  {
    label: 'Requests',
    used: 25,
    limit: 100,
    unit: 'requests',
    reset_at: '2026-09-11T12:00:00Z',
  },
  { label: 'Spend', used: 3, limit: 10, unit: 'USD', reset_at: null },
  { label: 'Tokens', used: 200, limit: 1000, unit: 'tokens', reset_at: null },
];

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
    vi.mocked(setProviderDisabled).mockResolvedValue({
      provider: 'zai',
      disabled: true,
      affected_model_count: 3,
    });
    vi.spyOn(window, 'confirm');
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    vi.restoreAllMocks();
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
      'https://github.com/HarvardMadSys/hybridInference/blob/dev/docs/developer/backend-extensions.md#quota-reporting',
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

  it.each(['first', 'last'])('renders every quota row when concurrency is %s', async (position) => {
    const usages =
      position === 'first'
        ? [CONCURRENCY_USAGE, ...QUOTA_USAGES]
        : [...QUOTA_USAGES, CONCURRENCY_USAGE];
    vi.mocked(getProviderQuotas).mockResolvedValueOnce({
      generated_at: ACTIVE_KEY.fetched_at,
      providers: [{ ...ACTIVE_KEY, usages }],
    });

    await openQuotas();

    expect(
      screen.getAllByText(/^(Concurrency|Requests|Spend|Tokens)$/).map((row) => row.textContent),
    ).toEqual(usages.map((usage) => usage.label));
    expect(screen.getByText('— / 10 slots')).toBeInTheDocument();
    expect(screen.getByText(/25 \/ 100 requests/)).toBeInTheDocument();
    expect(screen.getByText(/3 \/ 10 USD/)).toBeInTheDocument();
    expect(screen.getByText(/200 \/ 1,000 tokens/)).toBeInTheDocument();
    expect(screen.getByText('(25%)')).toBeInTheDocument();
    expect(screen.getByText('(30%)')).toBeInTheDocument();
    expect(screen.getByText('(20%)')).toBeInTheDocument();
    expect(screen.getByText(/^Resets at /)).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: 'Disable ZAI #1' })).toBeChecked();
    expect(
      screen.getByRole('switch', { name: 'Disable ZAI #1 key sk-zaiaaa...aaaa' }),
    ).toBeChecked();
  });

  it.each([
    ['slots', 'Concurrency 10 slots'],
    ['units', 'Concurrency 10 units'],
    ['', 'Concurrency 10'],
  ])('uses the source unit %j for a single concurrency result', async (unit, message) => {
    vi.mocked(getProviderQuotas).mockResolvedValueOnce({
      generated_at: ACTIVE_KEY.fetched_at,
      providers: [
        {
          ...ACTIVE_KEY,
          usages: [{ ...CONCURRENCY_USAGE, label: 'cOnCuRrEnCy', unit }],
        },
      ],
    });

    await openQuotas();

    expect(screen.getByText(message).textContent).toBe(message);
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
    expect(screen.queryByText(/— \/ 10/)).not.toBeInTheDocument();
  });

  it('keeps the availability message for a single concurrency result with an unknown limit', async () => {
    vi.mocked(getProviderQuotas).mockResolvedValueOnce({
      generated_at: ACTIVE_KEY.fetched_at,
      providers: [{ ...ACTIVE_KEY, usages: [{ ...CONCURRENCY_USAGE, limit: null }] }],
    });

    await openQuotas();

    expect(screen.getByText('Concurrency available')).toBeInTheDocument();
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
  });

  it('renders every concurrency row when more than one is returned', async () => {
    vi.mocked(getProviderQuotas).mockResolvedValueOnce({
      generated_at: ACTIVE_KEY.fetched_at,
      providers: [
        {
          ...ACTIVE_KEY,
          usages: [CONCURRENCY_USAGE, { ...CONCURRENCY_USAGE, used: 2, limit: 4, unit: 'units' }],
        },
      ],
    });

    await openQuotas();

    expect(screen.getAllByText('Concurrency')).toHaveLength(2);
    expect(screen.getByText('— / 10 slots')).toBeInTheDocument();
    expect(screen.getByText(/2 \/ 4 units/)).toBeInTheDocument();
    expect(screen.getByText('(50%)')).toBeInTheDocument();
  });

  it.each([
    {
      error: 'plan_api_disabled',
      usages: [],
      message: 'The current subscription plan does not have API access enabled.',
    },
    {
      error: 'probe_unavailable',
      usages: [],
      message: 'Status unavailable — no configured route to probe.',
    },
    {
      error: 'timeout',
      usages: [CONCURRENCY_USAGE],
      message: 'Unavailable — probe timed out.',
    },
    {
      error: 'auth_failed',
      usages: [CONCURRENCY_USAGE],
      message: 'Unavailable — auth failed.',
    },
    {
      error: 'not_configured',
      usages: [CONCURRENCY_USAGE],
      message: 'Not configured.',
    },
    {
      error: 'upstream_failed',
      usages: [CONCURRENCY_USAGE],
      message: 'Unavailable — upstream_failed',
    },
    {
      error: 'auth_failed',
      usages: [],
      message: 'Quota unavailable — auth_failed',
    },
  ])('preserves the error message: $message', async ({ error, usages, message }) => {
    vi.mocked(getProviderQuotas).mockResolvedValueOnce({
      generated_at: ACTIVE_KEY.fetched_at,
      providers: [{ ...ACTIVE_KEY, ok: false, error, usages }],
    });

    await openQuotas();

    expect(screen.getByText(message)).toBeInTheDocument();
    expect(screen.queryByText(/^Concurrency/)).not.toBeInTheDocument();
  });

  it('keeps mixed usage visible for a disabled provider and its key independently enabled', async () => {
    vi.mocked(getProviderQuotas).mockResolvedValueOnce({
      generated_at: ACTIVE_KEY.fetched_at,
      providers: [{ ...ACTIVE_KEY, disabled: true, usages: [CONCURRENCY_USAGE, ...QUOTA_USAGES] }],
    });

    await openQuotas();

    expect(screen.getByText('Disabled')).toBeInTheDocument();
    expect(screen.getByText('Concurrency')).toBeInTheDocument();
    expect(screen.getByText('Requests')).toBeInTheDocument();
    expect(screen.getByText('Spend')).toBeInTheDocument();
    expect(screen.getByText('Tokens')).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: 'Enable ZAI #1' })).not.toBeChecked();
    expect(
      screen.getByRole('switch', { name: 'Disable ZAI #1 key sk-zaiaaa...aaaa' }),
    ).toBeChecked();
  });

  it('keeps the disabled-key message ahead of any returned usage', async () => {
    vi.mocked(getProviderQuotas).mockResolvedValueOnce({
      generated_at: ACTIVE_KEY.fetched_at,
      providers: [
        {
          ...DISABLED_KEY,
          ok: true,
          error: null,
          usages: [CONCURRENCY_USAGE, ...QUOTA_USAGES],
        },
      ],
    });

    await openQuotas('ZAI #2');

    expect(
      screen.getByText('Key disabled — removed from the rotation pool and not used for inference.'),
    ).toBeInTheDocument();
    expect(screen.queryByText(/^(Concurrency|Requests|Spend|Tokens)/)).not.toBeInTheDocument();
    expect(screen.getByRole('switch', { name: 'Disable ZAI #2' })).toBeChecked();
    expect(
      screen.getByRole('switch', { name: 'Enable ZAI #2 key sk-zaibbb...bbbb' }),
    ).not.toBeChecked();
  });

  it('disables a single key on the click, with no confirmation, and reloads the quotas', async () => {
    await openQuotas();

    await clickSwitch('Disable ZAI #1 key sk-zaiaaa...aaaa');

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(vi.mocked(setProviderQuotaKeyDisabled).mock.calls).toEqual([
      ['zai', 'ref-active', true],
    ]);
    await waitFor(() => expect(getProviderQuotas).toHaveBeenCalledTimes(2));
    expect(toast.success).toHaveBeenCalledWith('Key disabled');
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it('re-enables a disabled key on the click', async () => {
    await openQuotas();

    expect(screen.getByText('Key disabled')).toBeInTheDocument();
    await clickSwitch('Enable ZAI #2 key sk-zaibbb...bbbb');

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(vi.mocked(setProviderQuotaKeyDisabled).mock.calls).toEqual([
      ['zai', 'ref-disabled', false],
    ]);
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Key enabled'));
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it('holds the switches while a key switch is in flight, says why it failed, and switches on a retry', async () => {
    let fail!: (error: Error) => void;
    vi.mocked(setProviderQuotaKeyDisabled)
      .mockImplementationOnce(
        () =>
          new Promise<ProviderKeyByRefResponse>((_, reject) => {
            fail = reject;
          }),
      )
      .mockResolvedValueOnce({
        provider: 'zai',
        key_ref: 'ref-active',
        source: 'db',
        status: 'disabled',
        pools_updated: 1,
      });
    await openQuotas();

    await clickSwitch('Disable ZAI #1 key sk-zaiaaa...aaaa');
    const keySwitch = screen.getByRole('switch', { name: 'Disable ZAI #1 key sk-zaiaaa...aaaa' });
    expect(keySwitch).toBeDisabled();
    expect(screen.getByRole('switch', { name: 'Disable ZAI #1' })).toBeDisabled();
    await clickSwitch('Disable ZAI #1 key sk-zaiaaa...aaaa');
    expect(setProviderQuotaKeyDisabled).toHaveBeenCalledTimes(1);

    await act(async () => fail(new Error('pool locked')));
    expect(toast.error).toHaveBeenCalledWith('pool locked');
    expect(screen.queryByRole('dialog')).toBeNull();
    // Nothing changed, so nothing is reloaded and the key still reads as enabled.
    expect(getProviderQuotas).toHaveBeenCalledTimes(1);
    expect(keySwitch).toBeChecked();
    expect(keySwitch).toBeEnabled();

    await clickSwitch('Disable ZAI #1 key sk-zaiaaa...aaaa');
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Key disabled'));
    expect(setProviderQuotaKeyDisabled).toHaveBeenCalledTimes(2);
  });

  it('offers no key toggle for cookie-based credentials', async () => {
    await openQuotas();

    expect(screen.queryByRole('switch', { name: /MiniMax key/ })).not.toBeInTheDocument();
    expect(screen.getByRole('switch', { name: 'Disable MiniMax' })).toBeInTheDocument();
  });

  it('keeps the provider-wide toggle separate from the key toggle', async () => {
    await openQuotas();

    await clickSwitch('Disable ZAI #1');

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(vi.mocked(setProviderDisabled).mock.calls).toEqual([['zai', true]]);
    expect(setProviderQuotaKeyDisabled).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getAllByText('Disabled').length).toBeGreaterThan(0));
    expect(toast.success).toHaveBeenCalledWith('ZAI #1 disabled — excluded from routing');
  });

  it('leaves the provider as it was when its switch fails, and says why', async () => {
    vi.mocked(setProviderDisabled).mockRejectedValueOnce(new Error('not allowed'));
    await openQuotas();

    await clickSwitch('Disable ZAI #1');

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('not allowed'));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByRole('switch', { name: 'Disable ZAI #1' })).toBeChecked();
    expect(screen.queryByText('Disabled')).not.toBeInTheDocument();
  });
});

describe('ProvidersTab availability', () => {
  beforeEach(() => {
    vi.mocked(getRoutableProviders).mockResolvedValue({
      providers: [
        {
          provider: 'zai',
          display_name: 'ZAI',
          model_count: 3,
          endpoint_count: 4,
          disabled: false,
        },
        {
          provider: 'openrouter',
          display_name: 'OpenRouter',
          model_count: 1,
          endpoint_count: 1,
          disabled: true,
        },
      ],
    });
    vi.mocked(setProviderDisabled).mockResolvedValue({
      provider: 'zai',
      disabled: true,
      affected_model_count: 3,
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  async function openAvailability() {
    render(<ProvidersTab />);
    fireEvent.click(screen.getByRole('tab', { name: 'Availability' }));
    await screen.findByRole('switch', { name: 'Disable zai' });
  }

  it('disables a provider on the click, with no confirmation', async () => {
    await openAvailability();

    await clickSwitch('Disable zai');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(vi.mocked(setProviderDisabled).mock.calls).toEqual([['zai', true]]);
    expect(await screen.findByRole('switch', { name: 'Enable zai' })).not.toBeChecked();
    expect(toast.success).toHaveBeenCalledWith('zai disabled — excluded from routing');
  });

  it('holds the switches while a provider switch is in flight, and switches once', async () => {
    let finish!: (value: Awaited<ReturnType<typeof setProviderDisabled>>) => void;
    vi.mocked(setProviderDisabled).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    await openAvailability();

    await clickSwitch('Disable zai');
    expect(screen.getByRole('switch', { name: 'Disable zai' })).toBeDisabled();
    expect(screen.getByRole('switch', { name: 'Enable openrouter' })).toBeDisabled();
    await clickSwitch('Disable zai');
    expect(setProviderDisabled).toHaveBeenCalledTimes(1);

    await act(async () => finish({ provider: 'zai', disabled: true, affected_model_count: 3 }));
    expect(screen.getByRole('switch', { name: 'Enable zai' })).toBeEnabled();
    expect(setProviderDisabled).toHaveBeenCalledTimes(1);
  });

  it('says why re-enabling a provider failed, leaves it disabled, and enables it on a retry', async () => {
    vi.mocked(setProviderDisabled)
      .mockRejectedValueOnce(new Error('not allowed'))
      .mockResolvedValueOnce({ provider: 'openrouter', disabled: false, affected_model_count: 1 });
    await openAvailability();

    await clickSwitch('Enable openrouter');
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('not allowed'));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByRole('switch', { name: 'Enable openrouter' })).not.toBeChecked();

    await clickSwitch('Enable openrouter');
    expect(await screen.findByRole('switch', { name: 'Disable openrouter' })).toBeChecked();
    expect(vi.mocked(setProviderDisabled).mock.calls).toEqual([
      ['openrouter', false],
      ['openrouter', false],
    ]);
  });
});
