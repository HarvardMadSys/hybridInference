// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ProviderKeysTab } from './ProviderKeysTab';

vi.mock('@/lib/api/admin', () => ({
  addProviderKey: vi.fn(),
  deleteProviderKey: vi.fn(),
  disableProviderEnvKey: vi.fn(),
  enableProviderEnvKey: vi.fn(),
  listProviderKeyProviders: vi.fn(),
  listProviderKeys: vi.fn(),
  setProviderEnvKeyMinRole: vi.fn(),
  setProviderKeyMinRole: vi.fn(),
  setProviderKeyStatus: vi.fn(),
  verifyProviderKey: vi.fn(),
}));

vi.mock('react-hot-toast', () => ({
  default: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

import {
  addProviderKey,
  listProviderKeyProviders,
  listProviderKeys,
  setProviderEnvKeyMinRole,
  setProviderKeyMinRole,
} from '@/lib/api/admin';

const dbKey = {
  id: 'key-1',
  provider: 'zai',
  key_prefix: 'sk-zai...1234',
  label: 'paid plan',
  source: 'db' as const,
  status: 'active',
  created_at: null,
  min_role: 'pro' as const,
};

const envKey = {
  id: 'env:abc',
  provider: 'zai',
  key_prefix: 'env-zai...5678',
  label: null,
  source: 'env' as const,
  status: 'active',
  created_at: null,
  min_role: 'free' as const,
};

function keyRow(prefix: string): HTMLElement {
  const cell = screen.getByText(prefix);
  const row = cell.closest('tr');
  if (!row) throw new Error(`no row for ${prefix}`);
  return row;
}

describe('ProviderKeysTab tier reservation', () => {
  beforeEach(() => {
    vi.mocked(listProviderKeyProviders).mockResolvedValue({ providers: ['zai'] });
    vi.mocked(listProviderKeys).mockResolvedValue({
      provider: 'zai',
      keys: [dbKey, envKey],
    });
    vi.mocked(setProviderKeyMinRole).mockResolvedValue({
      id: 'key-1',
      provider: 'zai',
      min_role: 'internal',
      pools_updated: 1,
    });
    vi.mocked(setProviderEnvKeyMinRole).mockResolvedValue({
      id: 'env:abc',
      provider: 'zai',
      min_role: 'pro',
      pools_updated: 1,
    });
    vi.mocked(addProviderKey).mockResolvedValue({
      key: { ...dbKey, id: 'key-2', min_role: 'pro' },
      pools_updated: 1,
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('shows a DB key reservation as an editable selector', async () => {
    render(<ProviderKeysTab />);

    const select = await screen.findByLabelText<HTMLSelectElement>(
      `Reserved tier for key ${dbKey.key_prefix}`,
    );
    expect(select.value).toBe('pro');
    expect(within(select).getByRole('option', { name: 'shared' })).toBeInTheDocument();
    expect(within(select).getByRole('option', { name: 'pro+' })).toBeInTheDocument();
  });

  it('re-tiers a key through the endpoint when the selector changes', async () => {
    render(<ProviderKeysTab />);

    const select = await screen.findByLabelText(`Reserved tier for key ${dbKey.key_prefix}`);
    fireEvent.change(select, { target: { value: 'internal' } });

    await waitFor(() => {
      expect(setProviderKeyMinRole).toHaveBeenCalledWith('key-1', 'internal');
    });
  });

  it('re-tiers an env key through the env endpoint', async () => {
    render(<ProviderKeysTab />);

    const select = await screen.findByLabelText<HTMLSelectElement>(
      `Reserved tier for key ${envKey.key_prefix}`,
    );
    expect(select.value).toBe('free');

    fireEvent.change(select, { target: { value: 'pro' } });

    await waitFor(() => {
      // Env keys are addressed by provider + env id, not by row id.
      expect(setProviderEnvKeyMinRole).toHaveBeenCalledWith('zai', 'env:abc', 'pro');
    });
    expect(setProviderKeyMinRole).not.toHaveBeenCalled();
  });

  it('shows a plain tier label for a key with no id', async () => {
    vi.mocked(listProviderKeys).mockResolvedValue({
      provider: 'zai',
      keys: [{ ...envKey, id: null, min_role: 'pro' }],
    });
    render(<ProviderKeysTab />);

    await screen.findByText(envKey.key_prefix);
    const row = keyRow(envKey.key_prefix);
    expect(within(row).queryByRole('combobox')).not.toBeInTheDocument();
    expect(within(row).getByText('pro+')).toBeInTheDocument();
  });

  it('submits the chosen tier when adding a key', async () => {
    render(<ProviderKeysTab />);

    const tier = await screen.findByLabelText('Reserved for');
    fireEvent.change(tier, { target: { value: 'pro' } });
    fireEvent.change(screen.getByLabelText('API key'), {
      target: { value: 'sk-zai-new-key-0123456789' },
    });
    fireEvent.submit(screen.getByRole('form', { name: 'Add a new key' }));

    await waitFor(() => {
      expect(addProviderKey).toHaveBeenCalledWith(
        'zai',
        'sk-zai-new-key-0123456789',
        undefined,
        'pro',
      );
    });
  });

  it('offers only the tier control for a reservation-only env row', async () => {
    // An active DB row holds the same credential, so this entry exists purely to
    // carry the reservation — the Disable endpoint rejects it by design.
    vi.mocked(listProviderKeys).mockResolvedValue({
      provider: 'zai',
      keys: [{ ...envKey, min_role: 'pro', reservation_only: true }],
    });
    render(<ProviderKeysTab />);

    await screen.findByText(envKey.key_prefix);
    const row = keyRow(envKey.key_prefix);
    expect(
      within(row).getByLabelText(`Reserved tier for key ${envKey.key_prefix}`),
    ).toBeInTheDocument();
    expect(within(row).queryByRole('button', { name: /disable/i })).not.toBeInTheDocument();
    expect(within(row).getByText('reservation only')).toBeInTheDocument();
  });

  it('edits the declared tier and flags the enforced one when they differ', async () => {
    vi.mocked(listProviderKeys).mockResolvedValue({
      provider: 'zai',
      keys: [{ ...dbKey, declared_min_role: 'free', min_role: 'internal' }],
    });
    render(<ProviderKeysTab />);

    const select = await screen.findByLabelText<HTMLSelectElement>(
      `Reserved tier for key ${dbKey.key_prefix}`,
    );
    // The selector shows what this row declares — that is what editing changes.
    expect(select.value).toBe('free');
    // ...and the tier actually in force is called out, so the edit does not look
    // like it did nothing.
    expect(screen.getByText('internal+ enforced')).toBeInTheDocument();
  });

  it('shows no enforced badge when the row is the only declaration', async () => {
    vi.mocked(listProviderKeys).mockResolvedValue({
      provider: 'zai',
      keys: [{ ...dbKey, declared_min_role: 'pro', min_role: 'pro' }],
    });
    render(<ProviderKeysTab />);

    await screen.findByLabelText(`Reserved tier for key ${dbKey.key_prefix}`);
    expect(screen.queryByText(/enforced/)).not.toBeInTheDocument();
  });
});
