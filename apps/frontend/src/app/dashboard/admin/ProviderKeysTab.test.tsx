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

import toast from 'react-hot-toast';
import {
  addProviderKey,
  deleteProviderKey,
  disableProviderEnvKey,
  enableProviderEnvKey,
  listProviderKeyProviders,
  listProviderKeys,
  setProviderEnvKeyMinRole,
  setProviderKeyMinRole,
  setProviderKeyStatus,
  verifyProviderKey,
} from '@/lib/api/admin';
import type { SetProviderApiKeyMinRoleResponse } from '@/lib/api/admin';
import { chooseValue, openOptions } from '@/test-utils/select';

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

const NEW_SECRET = 'sk-zai-new-key-0123456789';

function keyRow(prefix: string): HTMLElement {
  const cell = screen.getByText(prefix);
  const row = cell.closest('tr');
  if (!row) throw new Error(`no row for ${prefix}`);
  return row;
}

function dialog(): HTMLElement {
  return screen.getByRole('dialog');
}

function clickInDialog(name: string) {
  fireEvent.click(within(dialog()).getByRole('button', { name }));
}

async function renderTiers() {
  render(<ProviderKeysTab />);
  await screen.findAllByRole('combobox', { name: /^Reserved tier for key / });
}

function saveTiers() {
  fireEvent.click(screen.getByRole('button', { name: 'Save changes to zai key tiers' }));
}

async function openAddForm() {
  fireEvent.click(await screen.findByRole('button', { name: 'Add key' }));
  return screen.getByRole('form', { name: 'Add a new key' });
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
    vi.spyOn(window, 'confirm');
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    vi.restoreAllMocks();
  });

  it('initializes the key list and add form with the requested provider', async () => {
    vi.mocked(listProviderKeyProviders).mockResolvedValue({ providers: ['openrouter', 'zai'] });
    const onProviderChange = vi.fn();
    render(<ProviderKeysTab initialProvider="zai" onProviderChange={onProviderChange} />);

    await waitFor(() => expect(listProviderKeys).toHaveBeenCalledWith('zai'));
    expect(listProviderKeys).not.toHaveBeenCalledWith('openrouter');
    expect(screen.getByLabelText('Selected provider')).toHaveAttribute('data-value', 'zai');
    await openAddForm();
    expect(screen.getByLabelText('Provider')).toHaveAttribute('data-value', 'zai');

    chooseValue(screen.getByLabelText('Selected provider'), 'openrouter');
    await waitFor(() => expect(onProviderChange).toHaveBeenLastCalledWith('openrouter'));
  });

  it('preserves an unavailable requested provider without fetching its keys', async () => {
    render(<ProviderKeysTab initialProvider="config-only" />);

    expect(
      await screen.findByText(
        'Key management is unavailable for config-only. Choose another provider above.',
      ),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('Selected provider')).toHaveAttribute('data-value', 'config-only');
    expect(listProviderKeys).not.toHaveBeenCalled();
    expect(screen.queryByRole('form', { name: 'Add a new key' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add key' })).not.toBeInTheDocument();

    chooseValue(screen.getByLabelText('Selected provider'), 'zai');
    await waitFor(() => expect(listProviderKeys).toHaveBeenCalledWith('zai'));
    await openAddForm();
    expect(screen.getByLabelText('Provider')).toHaveAttribute('data-value', 'zai');
    expect(screen.getByRole('form', { name: 'Add a new key' })).toBeInTheDocument();
  });

  it('edits reservations in place, with no Edit step and no save bar until a change', async () => {
    render(<ProviderKeysTab />);

    const select = await screen.findByLabelText(`Reserved tier for key ${dbKey.key_prefix}`);
    expect(screen.queryByRole('button', { name: /^Edit tiers/ })).toBeNull();
    expect(select).toHaveAttribute('data-value', 'pro');
    expect(within(openOptions(select)).getByRole('option', { name: 'shared' })).toBeInTheDocument();
    expect(within(openOptions(select)).getByRole('option', { name: 'pro+' })).toBeInTheDocument();
    fireEvent.keyDown(select, { key: 'Escape' });
    expect(screen.queryByRole('button', { name: /^Save changes/ })).toBeNull();
    // Nothing is unsaved, so the provider can be switched.
    expect(screen.getByLabelText('Selected provider')).toBeEnabled();

    chooseValue(select, 'internal');
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Save changes to zai key tiers' })).toBeEnabled();
    expect(setProviderKeyMinRole).not.toHaveBeenCalled();
    // Drafts belong to this provider's keys, so switching provider waits.
    expect(screen.getByLabelText('Selected provider')).toBeDisabled();
  });

  it('keeps tier changes as drafts: Discard writes nothing and restores the saved tier', async () => {
    await renderTiers();
    chooseValue(screen.getByLabelText(`Reserved tier for key ${dbKey.key_prefix}`), 'internal');
    expect(setProviderKeyMinRole).not.toHaveBeenCalled();
    expect(screen.getByText('1 unsaved change')).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to zai key tiers' }));
    expect(screen.getByLabelText(`Reserved tier for key ${dbKey.key_prefix}`)).toHaveAttribute(
      'data-value',
      'pro',
    );
    expect(screen.queryByRole('button', { name: /^Save changes/ })).toBeNull();
    expect(setProviderKeyMinRole).not.toHaveBeenCalled();
    expect(setProviderEnvKeyMinRole).not.toHaveBeenCalled();
    // With the drafts gone, the provider can be switched again.
    expect(screen.getByLabelText('Selected provider')).not.toBeDisabled();
  });

  it('treats a tier changed back to its saved value as no change', async () => {
    await renderTiers();
    const select = screen.getByLabelText(`Reserved tier for key ${dbKey.key_prefix}`);
    chooseValue(select, 'internal');
    chooseValue(select, 'pro');
    expect(screen.queryByRole('button', { name: 'Save changes to zai key tiers' })).toBeNull();
    expect(screen.getByLabelText('Selected provider')).toBeEnabled();
  });

  it('re-tiers a key through the endpoint on Save, with no confirmation', async () => {
    await renderTiers();
    chooseValue(screen.getByLabelText(`Reserved tier for key ${dbKey.key_prefix}`), 'internal');
    expect(setProviderKeyMinRole).not.toHaveBeenCalled();

    saveTiers();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(vi.mocked(setProviderKeyMinRole).mock.calls).toEqual([['key-1', 'internal']]);
    // Nothing is left unsaved, so the save bar goes and, once the keys are
    // reloaded, the provider is free again.
    await waitFor(() => expect(screen.getByLabelText('Selected provider')).toBeEnabled());
    expect(screen.queryByRole('button', { name: /^Save changes/ })).toBeNull();
    expect(setProviderEnvKeyMinRole).not.toHaveBeenCalled();
    expect(toast.success).toHaveBeenCalledWith('Key reserved for internal and above');
    expect(listProviderKeys).toHaveBeenCalledTimes(2);
  });

  it('holds the tier bar while a key action runs, then saves the tier', async () => {
    let finish!: (value: Awaited<ReturnType<typeof disableProviderEnvKey>>) => void;
    vi.mocked(disableProviderEnvKey).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    await renderTiers();
    chooseValue(screen.getByLabelText(`Reserved tier for key ${dbKey.key_prefix}`), 'internal');
    fireEvent.click(screen.getByRole('button', { name: `Disable key ${envKey.key_prefix}` }));
    expect(disableProviderEnvKey).toHaveBeenCalledTimes(1);

    // Not the bar being saved, so it does not say so; it waits for the key action.
    const save = screen.getByRole('button', { name: 'Save changes to zai key tiers' });
    expect(save).toHaveTextContent('Save changes');
    expect(save).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Discard changes to zai key tiers' })).toBeDisabled();
    fireEvent.click(save);
    expect(setProviderKeyMinRole).not.toHaveBeenCalled();

    finish({ provider: 'zai', id: envKey.id, disabled: true, pools_updated: 1 } as never);
    await waitFor(() => expect(save).toBeEnabled());
    saveTiers();
    await waitFor(() => expect(setProviderKeyMinRole).toHaveBeenCalledWith('key-1', 'internal'));
  });

  it('drafts a tier only on a deliberate choice of a different tier, and never writes from the select', async () => {
    await renderTiers();

    const select = screen.getByLabelText(`Reserved tier for key ${dbKey.key_prefix}`);
    // Browsing and dismissing changes nothing.
    fireEvent.keyDown(select, { key: 'ArrowDown' });
    fireEvent.keyDown(select, { key: 'ArrowDown' });
    fireEvent.keyDown(select, { key: 'Escape' });
    // Nor does choosing the tier it already has.
    chooseValue(select, 'pro');
    expect(screen.queryByRole('button', { name: 'Save changes to zai key tiers' })).toBeNull();

    // A different tier is a draft, still not a write.
    chooseValue(select, 'internal');
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    expect(setProviderKeyMinRole).not.toHaveBeenCalled();
    expect(setProviderEnvKeyMinRole).not.toHaveBeenCalled();

    saveTiers();
    await waitFor(() => expect(setProviderKeyMinRole).toHaveBeenCalledTimes(1));
  });

  it('re-tiers an env key through the env endpoint', async () => {
    await renderTiers();
    const select = screen.getByLabelText(`Reserved tier for key ${envKey.key_prefix}`);
    expect(select).toHaveAttribute('data-value', 'free');

    chooseValue(select, 'pro');
    expect(setProviderEnvKeyMinRole).not.toHaveBeenCalled();
    saveTiers();

    await waitFor(() => {
      // Env keys are addressed by provider + env id, not by row id.
      expect(setProviderEnvKeyMinRole).toHaveBeenCalledWith('zai', 'env:abc', 'pro');
    });
    expect(setProviderEnvKeyMinRole).toHaveBeenCalledTimes(1);
    expect(setProviderKeyMinRole).not.toHaveBeenCalled();
  });

  it('saves several tier changes with one request per key and warns about an unpooled key', async () => {
    vi.mocked(setProviderEnvKeyMinRole).mockResolvedValueOnce({
      id: 'env:abc',
      provider: 'zai',
      min_role: 'admin',
      pools_updated: 0,
    });
    await renderTiers();
    chooseValue(screen.getByLabelText(`Reserved tier for key ${dbKey.key_prefix}`), 'free');
    chooseValue(screen.getByLabelText(`Reserved tier for key ${envKey.key_prefix}`), 'admin');
    expect(screen.getByText('2 unsaved changes')).toBeVisible();
    saveTiers();

    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith('Updated reserved tiers for 2 keys'),
    );
    expect(vi.mocked(setProviderKeyMinRole).mock.calls).toEqual([['key-1', 'free']]);
    expect(vi.mocked(setProviderEnvKeyMinRole).mock.calls).toEqual([['zai', 'env:abc', 'admin']]);
    expect(toast.error).toHaveBeenCalledWith(
      'Saved, but no live pool holds env-zai...5678 right now — the new tier applies once it is back in rotation.',
    );
  });

  it('holds a second Save, Discard, the tiers and the provider while the tier save is in flight', async () => {
    let finish!: (value: SetProviderApiKeyMinRoleResponse) => void;
    vi.mocked(setProviderKeyMinRole).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    await renderTiers();
    chooseValue(screen.getByLabelText(`Reserved tier for key ${dbKey.key_prefix}`), 'internal');
    saveTiers();

    const saving = screen.getByRole('button', { name: 'Save changes to zai key tiers' });
    expect(saving).toHaveTextContent('Saving…');
    expect(saving).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Discard changes to zai key tiers' })).toBeDisabled();
    expect(screen.getByLabelText(`Reserved tier for key ${envKey.key_prefix}`)).toBeDisabled();
    expect(screen.getByLabelText('Selected provider')).toBeDisabled();
    fireEvent.click(saving);
    expect(setProviderKeyMinRole).toHaveBeenCalledTimes(1);

    finish({ id: 'key-1', provider: 'zai', min_role: 'internal', pools_updated: 1 });
    await waitFor(() => expect(screen.getByLabelText('Selected provider')).toBeEnabled());
    expect(screen.queryByRole('button', { name: /^Save changes/ })).toBeNull();
    expect(setProviderKeyMinRole).toHaveBeenCalledTimes(1);
  });

  it('keeps the drafts and shows the error after a failed tier save, and saves them again', async () => {
    vi.mocked(setProviderKeyMinRole).mockRejectedValueOnce(new Error('write failed'));
    await renderTiers();
    chooseValue(screen.getByLabelText(`Reserved tier for key ${dbKey.key_prefix}`), 'internal');
    saveTiers();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'sk-zai...1234 (paid plan) could not be saved: write failed',
    );
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByLabelText(`Reserved tier for key ${dbKey.key_prefix}`)).toHaveAttribute(
      'data-value',
      'internal',
    );
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    // Nothing was written, so nothing is reloaded.
    expect(listProviderKeys).toHaveBeenCalledTimes(1);

    saveTiers();
    await waitFor(() => expect(screen.getByLabelText('Selected provider')).toBeEnabled());
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByRole('button', { name: /^Save changes/ })).toBeNull();
    expect(setProviderKeyMinRole).toHaveBeenCalledTimes(2);
  });

  it('reports a partial tier save and saves only the unsaved key again', async () => {
    vi.mocked(setProviderEnvKeyMinRole).mockRejectedValueOnce(new Error('write failed'));
    await renderTiers();
    chooseValue(screen.getByLabelText(`Reserved tier for key ${dbKey.key_prefix}`), 'internal');
    chooseValue(screen.getByLabelText(`Reserved tier for key ${envKey.key_prefix}`), 'pro');
    saveTiers();

    expect(
      await screen.findByText(/env-zai\.\.\.5678 could not be saved: write failed/),
    ).toHaveTextContent('1 change was saved; the rest are still unsaved.');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByText('1 unsaved change')).toBeVisible();

    saveTiers();
    await waitFor(() => expect(screen.queryByRole('button', { name: /^Save changes/ })).toBeNull());
    expect(setProviderKeyMinRole).toHaveBeenCalledTimes(1);
    expect(vi.mocked(setProviderEnvKeyMinRole).mock.calls).toEqual([
      ['zai', 'env:abc', 'pro'],
      ['zai', 'env:abc', 'pro'],
    ]);
  });

  it('shows a plain tier label for a key with no id', async () => {
    vi.mocked(listProviderKeys).mockResolvedValue({
      provider: 'zai',
      keys: [dbKey, { ...envKey, id: null, min_role: 'pro' }],
    });
    await renderTiers();

    const row = keyRow(envKey.key_prefix);
    expect(within(row).queryByRole('combobox')).not.toBeInTheDocument();
    expect(within(row).getByText('pro+')).toBeInTheDocument();
  });

  it('offers no tier editing when no key has an id', async () => {
    vi.mocked(listProviderKeys).mockResolvedValue({
      provider: 'zai',
      keys: [{ ...envKey, id: null, min_role: 'pro' }],
    });
    render(<ProviderKeysTab />);

    await screen.findByText(envKey.key_prefix);
    expect(screen.queryByRole('combobox', { name: /^Reserved tier/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Save changes/ })).not.toBeInTheDocument();
  });

  it('opens the add form only from Add key, and Cancel closes it without writing', async () => {
    render(<ProviderKeysTab />);
    await screen.findByText(dbKey.key_prefix);
    expect(screen.queryByRole('form', { name: 'Add a new key' })).not.toBeInTheDocument();

    await openAddForm();
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: NEW_SECRET } });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByRole('form', { name: 'Add a new key' })).not.toBeInTheDocument();
    expect(addProviderKey).not.toHaveBeenCalled();
    await openAddForm();
    expect(screen.getByLabelText('API key')).toHaveValue('');
  });

  it('adds the key with the chosen tier on Add key, with no confirmation', async () => {
    render(<ProviderKeysTab />);
    const form = await openAddForm();

    chooseValue(screen.getByLabelText('Reserved for'), 'pro');
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: NEW_SECRET } });
    expect(addProviderKey).not.toHaveBeenCalled();

    fireEvent.click(within(form).getByRole('button', { name: 'Add key' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(vi.mocked(addProviderKey).mock.calls).toEqual([['zai', NEW_SECRET, undefined, 'pro']]);
    await waitFor(() =>
      expect(screen.queryByRole('form', { name: 'Add a new key' })).not.toBeInTheDocument(),
    );
    expect(toast.success).toHaveBeenCalledWith('Key added (1 pool(s) updated)');
  });

  it('holds the form while the key is being added, and adds it once', async () => {
    let finish!: (value: Awaited<ReturnType<typeof addProviderKey>>) => void;
    vi.mocked(addProviderKey).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    render(<ProviderKeysTab />);
    const form = await openAddForm();
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: NEW_SECRET } });
    fireEvent.submit(form);

    const adding = within(form).getByRole('button', { name: 'Adding…' });
    expect(adding).toBeDisabled();
    expect(within(form).getByRole('button', { name: 'Cancel' })).toBeDisabled();
    expect(screen.getByLabelText('API key')).toBeDisabled();
    fireEvent.submit(form);
    expect(addProviderKey).toHaveBeenCalledTimes(1);

    finish({ key: { ...dbKey, id: 'key-2' }, pools_updated: 1 });
    await waitFor(() => expect(form).not.toBeInTheDocument());
    expect(addProviderKey).toHaveBeenCalledTimes(1);
  });

  it('keeps the form with a masked error when adding fails, and adds the key on a retry', async () => {
    vi.mocked(addProviderKey)
      .mockRejectedValueOnce(new Error(`key ${NEW_SECRET} was rejected`))
      .mockResolvedValueOnce({ key: { ...dbKey, id: 'key-2' }, pools_updated: 1 });
    render(<ProviderKeysTab />);
    const form = await openAddForm();
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: NEW_SECRET } });
    fireEvent.submit(form);

    const alert = await within(form).findByRole('alert');
    expect(alert).toHaveTextContent('key •••• was rejected');
    expect(alert).not.toHaveTextContent(NEW_SECRET);
    expect(toast.error).not.toHaveBeenCalledWith(expect.stringContaining(NEW_SECRET));
    expect(screen.getByLabelText('API key')).toHaveValue(NEW_SECRET);

    fireEvent.submit(form);
    await waitFor(() => expect(form).not.toBeInTheDocument());
    expect(addProviderKey).toHaveBeenCalledTimes(2);
  });

  // Adding a key switches the page to the key's provider, which would leave
  // unsaved tier drafts for the provider shown now with no Save or Discard.
  describe('while tier changes are unsaved', () => {
    const LOCKED = 'Save or discard the tier changes first';

    beforeEach(() => {
      vi.mocked(listProviderKeyProviders).mockResolvedValue({ providers: ['chutes', 'zai'] });
      vi.mocked(addProviderKey).mockResolvedValue({
        key: { ...dbKey, id: 'key-3', provider: 'chutes' },
        pools_updated: 1,
      });
    });

    it('holds Add key until the tier changes are saved or discarded', async () => {
      render(<ProviderKeysTab initialProvider="zai" />);
      const addKey = await screen.findByRole('button', { name: 'Add key' });
      expect(addKey).toBeEnabled();
      chooseValue(
        await screen.findByLabelText(`Reserved tier for key ${dbKey.key_prefix}`),
        'internal',
      );
      expect(screen.getByText('1 unsaved change')).toBeVisible();

      expect(addKey).toBeDisabled();
      expect(addKey).toHaveAttribute('title', LOCKED);

      fireEvent.click(screen.getByRole('button', { name: 'Discard changes to zai key tiers' }));
      expect(addKey).toBeEnabled();
      expect(addKey).not.toHaveAttribute('title');
    });

    it('holds adding a key for another provider while tier changes made after it are unsaved', async () => {
      render(<ProviderKeysTab initialProvider="zai" />);
      const form = await openAddForm();
      chooseValue(screen.getByLabelText('Provider'), 'chutes');
      fireEvent.change(screen.getByLabelText('API key'), { target: { value: NEW_SECRET } });
      chooseValue(
        await screen.findByLabelText(`Reserved tier for key ${dbKey.key_prefix}`),
        'internal',
      );

      const add = within(form).getByRole('button', { name: 'Add key' });
      expect(add).toBeDisabled();
      expect(add).toHaveAttribute('title', LOCKED);
      fireEvent.submit(form);
      expect(addProviderKey).not.toHaveBeenCalled();
      expect(screen.getByLabelText('Selected provider')).toHaveAttribute('data-value', 'zai');
      expect(screen.getByText('1 unsaved change')).toBeVisible();

      // Once the tier changes are discarded, the key is added as before.
      fireEvent.click(screen.getByRole('button', { name: 'Discard changes to zai key tiers' }));
      expect(add).toBeEnabled();
      fireEvent.submit(form);
      await waitFor(() =>
        expect(screen.getByLabelText('Selected provider')).toHaveAttribute('data-value', 'chutes'),
      );
      expect(vi.mocked(addProviderKey).mock.calls).toEqual([
        ['chutes', NEW_SECRET, undefined, 'free'],
      ]);
      expect(setProviderKeyMinRole).not.toHaveBeenCalled();
    });
  });

  it('verifies a key directly, without a confirmation', async () => {
    vi.mocked(verifyProviderKey).mockResolvedValue({ ok: true });
    render(<ProviderKeysTab />);
    await openAddForm();
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: NEW_SECRET } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));

    await waitFor(() => expect(verifyProviderKey).toHaveBeenCalledWith('zai', NEW_SECRET));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(await screen.findByRole('button', { name: 'Verified' })).toBeInTheDocument();
  });

  it('deletes a DB key only after the named confirmation', async () => {
    vi.mocked(deleteProviderKey).mockResolvedValue({
      id: 'key-1',
      provider: 'zai',
      pools_updated: 1,
    });
    render(<ProviderKeysTab />);

    fireEvent.click(await screen.findByRole('button', { name: `Delete key ${dbKey.key_prefix}` }));
    const confirm = screen.getByRole('dialog', { name: `Delete key ${dbKey.key_prefix}?` });
    expect(confirm).toHaveTextContent('paid plan');
    expect(confirm).toHaveTextContent('This cannot be undone.');
    clickInDialog('Cancel');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(deleteProviderKey).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: `Delete key ${dbKey.key_prefix}` }));
    clickInDialog('Delete');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(vi.mocked(deleteProviderKey).mock.calls).toEqual([['key-1']]);
    expect(toast.success).toHaveBeenCalledWith('Key deleted');
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it('brings up no tier save bar while a key action is in flight', async () => {
    let finish!: (value: Awaited<ReturnType<typeof deleteProviderKey>>) => void;
    vi.mocked(deleteProviderKey).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    render(<ProviderKeysTab />);

    fireEvent.click(await screen.findByRole('button', { name: `Delete key ${dbKey.key_prefix}` }));
    clickInDialog('Delete');
    expect(within(dialog()).getByRole('button', { name: 'Deleting…' })).toBeDisabled();
    // The confirmation is shared, but no tier is changed, so there is nothing to save.
    expect(screen.getByLabelText(`Reserved tier for key ${dbKey.key_prefix}`)).toBeDisabled();
    expect(screen.queryByRole('button', { name: /^Save changes/ })).toBeNull();

    finish({ id: 'key-1', provider: 'zai', pools_updated: 1 });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('keeps the delete confirmation open with the error when deleting fails', async () => {
    vi.mocked(deleteProviderKey).mockRejectedValueOnce(new Error('in use'));
    render(<ProviderKeysTab />);

    fireEvent.click(await screen.findByRole('button', { name: `Delete key ${dbKey.key_prefix}` }));
    clickInDialog('Delete');

    expect(await within(dialog()).findByRole('alert')).toHaveTextContent('in use');
    expect(deleteProviderKey).toHaveBeenCalledTimes(1);
  });

  it('disables a DB key on the click, with no confirmation', async () => {
    vi.mocked(setProviderKeyStatus).mockResolvedValue({
      id: 'key-1',
      provider: 'zai',
      status: 'disabled',
      pools_updated: 1,
    });
    render(<ProviderKeysTab />);

    fireEvent.click(await screen.findByRole('button', { name: `Disable key ${dbKey.key_prefix}` }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(vi.mocked(setProviderKeyStatus).mock.calls).toEqual([['key-1', false]]);
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Key disabled'));
    await waitFor(() => expect(listProviderKeys).toHaveBeenCalledTimes(2));
  });

  it('holds the provider and every key action while a key action is in flight', async () => {
    let finish!: (value: Awaited<ReturnType<typeof setProviderKeyStatus>>) => void;
    vi.mocked(setProviderKeyStatus).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    render(<ProviderKeysTab />);

    const disable = await screen.findByRole('button', { name: `Disable key ${dbKey.key_prefix}` });
    fireEvent.click(disable);
    expect(disable).toBeDisabled();
    expect(screen.getByRole('button', { name: `Delete key ${dbKey.key_prefix}` })).toBeDisabled();
    expect(screen.getByRole('button', { name: `Disable key ${envKey.key_prefix}` })).toBeDisabled();
    // The key list it reloads belongs to this provider.
    expect(screen.getByLabelText('Selected provider')).toBeDisabled();
    fireEvent.click(disable);
    expect(setProviderKeyStatus).toHaveBeenCalledTimes(1);
    // No tier is changed, so there is nothing to save.
    expect(screen.queryByRole('button', { name: /^Save changes/ })).toBeNull();

    finish({ id: 'key-1', provider: 'zai', status: 'disabled', pools_updated: 1 });
    await waitFor(() => expect(screen.getByLabelText('Selected provider')).toBeEnabled());
    expect(setProviderKeyStatus).toHaveBeenCalledTimes(1);
  });

  it('says why a key action failed, and the same action can be tried again', async () => {
    vi.mocked(setProviderKeyStatus)
      .mockRejectedValueOnce(new Error('pool locked'))
      .mockResolvedValueOnce({
        id: 'key-1',
        provider: 'zai',
        status: 'disabled',
        pools_updated: 1,
      });
    render(<ProviderKeysTab />);

    const disable = await screen.findByRole('button', { name: `Disable key ${dbKey.key_prefix}` });
    fireEvent.click(disable);
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Disable failed: pool locked'));
    expect(screen.queryByRole('dialog')).toBeNull();
    // Nothing was written, so nothing is reloaded.
    expect(listProviderKeys).toHaveBeenCalledTimes(1);

    await waitFor(() => expect(disable).toBeEnabled());
    fireEvent.click(disable);
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Key disabled'));
    expect(vi.mocked(setProviderKeyStatus).mock.calls).toEqual([
      ['key-1', false],
      ['key-1', false],
    ]);
  });

  it('disables an env key on the click, with no confirmation', async () => {
    vi.mocked(disableProviderEnvKey).mockResolvedValue({
      id: 'env:abc',
      provider: 'zai',
      pools_updated: 1,
    });
    render(<ProviderKeysTab />);

    fireEvent.click(
      await screen.findByRole('button', { name: `Disable key ${envKey.key_prefix}` }),
    );
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(vi.mocked(disableProviderEnvKey).mock.calls).toEqual([['zai', 'env:abc']]);
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Env key disabled'));
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it('re-enables a disabled env key on the click', async () => {
    vi.mocked(listProviderKeys).mockResolvedValue({
      provider: 'zai',
      keys: [{ ...envKey, status: 'disabled' }],
    });
    vi.mocked(enableProviderEnvKey).mockResolvedValue({
      id: 'env:abc',
      provider: 'zai',
      pools_updated: 0,
    });
    render(<ProviderKeysTab />);

    fireEvent.click(await screen.findByRole('button', { name: `Enable key ${envKey.key_prefix}` }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(vi.mocked(enableProviderEnvKey).mock.calls).toEqual([['zai', 'env:abc']]);
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'Env key re-enabled but could not be re-added to any pool.',
      ),
    );
  });

  it('offers only the tier control for a reservation-only env row', async () => {
    // An active DB row holds the same credential, so this entry exists purely to
    // carry the reservation — the Disable endpoint rejects it by design.
    vi.mocked(listProviderKeys).mockResolvedValue({
      provider: 'zai',
      keys: [{ ...envKey, min_role: 'pro', reservation_only: true }],
    });
    await renderTiers();

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

    const select = await screen.findByLabelText(`Reserved tier for key ${dbKey.key_prefix}`);
    // The selector shows what this row declares — that is what editing changes.
    expect(select).toHaveAttribute('data-value', 'free');
    expect(select).toHaveTextContent('shared');
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
