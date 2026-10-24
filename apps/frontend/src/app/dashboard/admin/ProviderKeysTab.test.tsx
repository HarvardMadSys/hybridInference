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

async function openTierEditor() {
  render(<ProviderKeysTab />);
  fireEvent.click(await screen.findByRole('button', { name: 'Edit tiers zai key tiers' }));
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
    expect(screen.getByLabelText('Selected provider')).toHaveValue('zai');
    await openAddForm();
    expect(screen.getByLabelText('Provider')).toHaveValue('zai');

    fireEvent.change(screen.getByLabelText('Selected provider'), {
      target: { value: 'openrouter' },
    });
    await waitFor(() => expect(onProviderChange).toHaveBeenLastCalledWith('openrouter'));
  });

  it('preserves an unavailable requested provider without fetching its keys', async () => {
    render(<ProviderKeysTab initialProvider="config-only" />);

    expect(
      await screen.findByText(
        'Key management is unavailable for config-only. Choose another provider above.',
      ),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('Selected provider')).toHaveValue('config-only');
    expect(listProviderKeys).not.toHaveBeenCalled();
    expect(screen.queryByRole('form', { name: 'Add a new key' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add key' })).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Selected provider'), { target: { value: 'zai' } });
    await waitFor(() => expect(listProviderKeys).toHaveBeenCalledWith('zai'));
    await openAddForm();
    expect(screen.getByLabelText('Provider')).toHaveValue('zai');
    expect(screen.getByRole('form', { name: 'Add a new key' })).toBeInTheDocument();
  });

  it('shows reservations read-only until Edit, then as editable selectors', async () => {
    render(<ProviderKeysTab />);

    const value = await screen.findByLabelText(`Reserved tier for key ${dbKey.key_prefix}`);
    expect(value.tagName).toBe('OUTPUT');
    expect(value).toHaveTextContent('pro+');
    expect(screen.queryByRole('combobox', { name: /Reserved tier/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /^Save changes/ })).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Edit tiers zai key tiers' }));
    const select = screen.getByLabelText<HTMLSelectElement>(
      `Reserved tier for key ${dbKey.key_prefix}`,
    );
    expect(select.value).toBe('pro');
    expect(within(select).getByRole('option', { name: 'shared' })).toBeInTheDocument();
    expect(within(select).getByRole('option', { name: 'pro+' })).toBeInTheDocument();
    expect(screen.getByText('No changes yet')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Save changes to zai key tiers' })).toBeDisabled();
    // Drafts belong to this provider's keys, so switching provider waits.
    expect(screen.getByLabelText('Selected provider')).toBeDisabled();
  });

  it('keeps tier changes as drafts: Cancel writes nothing and restores the saved tier', async () => {
    await openTierEditor();
    fireEvent.change(screen.getByLabelText(`Reserved tier for key ${dbKey.key_prefix}`), {
      target: { value: 'internal' },
    });
    expect(screen.getByText('1 unsaved change')).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: 'Cancel editing zai key tiers' }));
    expect(screen.getByLabelText(`Reserved tier for key ${dbKey.key_prefix}`)).toHaveTextContent(
      'pro+',
    );
    expect(setProviderKeyMinRole).not.toHaveBeenCalled();
    expect(setProviderEnvKeyMinRole).not.toHaveBeenCalled();
  });

  it('treats a tier changed back to its saved value as no change', async () => {
    await openTierEditor();
    const select = screen.getByLabelText(`Reserved tier for key ${dbKey.key_prefix}`);
    fireEvent.change(select, { target: { value: 'internal' } });
    fireEvent.change(select, { target: { value: 'pro' } });
    expect(screen.getByRole('button', { name: 'Save changes to zai key tiers' })).toBeDisabled();
  });

  it('re-tiers a key through the endpoint only after the change is confirmed', async () => {
    await openTierEditor();
    fireEvent.change(screen.getByLabelText(`Reserved tier for key ${dbKey.key_prefix}`), {
      target: { value: 'internal' },
    });
    saveTiers();

    const review = screen.getByRole('dialog', { name: 'Save 1 tier change for zai keys?' });
    expect(review).toHaveTextContent('sk-zai...1234 (paid plan)pro+ → internal+');
    expect(setProviderKeyMinRole).not.toHaveBeenCalled();

    clickInDialog('Back to editing');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByLabelText(`Reserved tier for key ${dbKey.key_prefix}`)).toHaveValue(
      'internal',
    );
    expect(setProviderKeyMinRole).not.toHaveBeenCalled();

    saveTiers();
    clickInDialog('Confirm save');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(vi.mocked(setProviderKeyMinRole).mock.calls).toEqual([['key-1', 'internal']]);
    expect(setProviderEnvKeyMinRole).not.toHaveBeenCalled();
    expect(toast.success).toHaveBeenCalledWith('Key reserved for internal and above');
    expect(screen.getByLabelText(`Reserved tier for key ${dbKey.key_prefix}`).tagName).toBe(
      'OUTPUT',
    );
  });

  it('re-tiers an env key through the env endpoint', async () => {
    await openTierEditor();
    const select = screen.getByLabelText<HTMLSelectElement>(
      `Reserved tier for key ${envKey.key_prefix}`,
    );
    expect(select.value).toBe('free');

    fireEvent.change(select, { target: { value: 'pro' } });
    saveTiers();
    expect(dialog()).toHaveTextContent('env-zai...5678shared → pro+');
    clickInDialog('Confirm save');

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
    await openTierEditor();
    fireEvent.change(screen.getByLabelText(`Reserved tier for key ${dbKey.key_prefix}`), {
      target: { value: 'free' },
    });
    fireEvent.change(screen.getByLabelText(`Reserved tier for key ${envKey.key_prefix}`), {
      target: { value: 'admin' },
    });
    expect(screen.getByText('2 unsaved changes')).toBeVisible();
    saveTiers();
    expect(screen.getByRole('dialog', { name: 'Save 2 tier changes for zai keys?' })).toBeVisible();
    clickInDialog('Confirm save');

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(vi.mocked(setProviderKeyMinRole).mock.calls).toEqual([['key-1', 'free']]);
    expect(vi.mocked(setProviderEnvKeyMinRole).mock.calls).toEqual([['zai', 'env:abc', 'admin']]);
    expect(toast.success).toHaveBeenCalledWith('Updated reserved tiers for 2 keys');
    expect(toast.error).toHaveBeenCalledWith(
      'Saved, but no live pool holds env-zai...5678 right now — the new tier applies once it is back in rotation.',
    );
  });

  it('blocks a second Confirm and closing while the tier save is in flight', async () => {
    let finish!: (value: SetProviderApiKeyMinRoleResponse) => void;
    vi.mocked(setProviderKeyMinRole).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    await openTierEditor();
    fireEvent.change(screen.getByLabelText(`Reserved tier for key ${dbKey.key_prefix}`), {
      target: { value: 'internal' },
    });
    saveTiers();
    clickInDialog('Confirm save');

    expect(within(dialog()).getByRole('button', { name: 'Saving…' })).toBeDisabled();
    expect(within(dialog()).getByRole('button', { name: 'Back to editing' })).toBeDisabled();
    fireEvent.click(within(dialog()).getByRole('button', { name: 'Saving…' }));
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(setProviderKeyMinRole).toHaveBeenCalledTimes(1);

    finish({ id: 'key-1', provider: 'zai', min_role: 'internal', pools_updated: 1 });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(setProviderKeyMinRole).toHaveBeenCalledTimes(1);
  });

  it('keeps the dialog and drafts after a failed tier save, and retries from the dialog', async () => {
    vi.mocked(setProviderKeyMinRole).mockRejectedValueOnce(new Error('write failed'));
    await openTierEditor();
    fireEvent.change(screen.getByLabelText(`Reserved tier for key ${dbKey.key_prefix}`), {
      target: { value: 'internal' },
    });
    saveTiers();
    clickInDialog('Confirm save');

    expect(await within(dialog()).findByRole('alert')).toHaveTextContent(
      'sk-zai...1234 (paid plan) could not be saved: write failed',
    );
    expect(screen.getByLabelText(`Reserved tier for key ${dbKey.key_prefix}`)).toHaveValue(
      'internal',
    );

    clickInDialog('Confirm save');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(setProviderKeyMinRole).toHaveBeenCalledTimes(2);
  });

  it('reports a partial tier save and retries only the unsaved key', async () => {
    vi.mocked(setProviderEnvKeyMinRole).mockRejectedValueOnce(new Error('write failed'));
    await openTierEditor();
    fireEvent.change(screen.getByLabelText(`Reserved tier for key ${dbKey.key_prefix}`), {
      target: { value: 'internal' },
    });
    fireEvent.change(screen.getByLabelText(`Reserved tier for key ${envKey.key_prefix}`), {
      target: { value: 'pro' },
    });
    saveTiers();
    clickInDialog('Confirm save');

    expect(
      await screen.findByText(/env-zai\.\.\.5678 could not be saved: write failed/),
    ).toHaveTextContent('1 change was saved; the rest are still unsaved.');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByText('1 unsaved change')).toBeVisible();

    saveTiers();
    expect(
      screen.getByRole('dialog', { name: 'Save 1 tier change for zai keys?' }),
    ).not.toHaveTextContent('sk-zai...1234');
    clickInDialog('Confirm save');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
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
    await openTierEditor();

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
    expect(screen.queryByRole('button', { name: /^Edit tiers/ })).not.toBeInTheDocument();
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

  it('reviews the new key without its secret and submits the chosen tier on confirm', async () => {
    render(<ProviderKeysTab />);
    await openAddForm();

    fireEvent.change(screen.getByLabelText('Reserved for'), { target: { value: 'pro' } });
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: NEW_SECRET } });
    fireEvent.submit(screen.getByRole('form', { name: 'Add a new key' }));

    const review = screen.getByRole('dialog', { name: 'Add a key to zai?' });
    expect(review).toHaveTextContent('Providerzai');
    expect(review).toHaveTextContent('Label(none)');
    expect(review).toHaveTextContent('Reserved forpro+');
    expect(review).toHaveTextContent('API keySet (hidden)');
    expect(review).toHaveTextContent('VerifiedNot verified');
    expect(review).not.toHaveTextContent(NEW_SECRET);
    expect(addProviderKey).not.toHaveBeenCalled();

    clickInDialog('Back to editing');
    expect(screen.getByLabelText('API key')).toHaveValue(NEW_SECRET);
    expect(addProviderKey).not.toHaveBeenCalled();

    fireEvent.submit(screen.getByRole('form', { name: 'Add a new key' }));
    clickInDialog('Add key');

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(vi.mocked(addProviderKey).mock.calls).toEqual([['zai', NEW_SECRET, undefined, 'pro']]);
    expect(toast.success).toHaveBeenCalledWith('Key added (1 pool(s) updated)');
    expect(screen.queryByRole('form', { name: 'Add a new key' })).not.toBeInTheDocument();
  });

  it('keeps the add review open with a masked error when adding fails, and retries', async () => {
    vi.mocked(addProviderKey)
      .mockRejectedValueOnce(new Error(`key ${NEW_SECRET} was rejected`))
      .mockResolvedValueOnce({ key: { ...dbKey, id: 'key-2' }, pools_updated: 1 });
    render(<ProviderKeysTab />);
    await openAddForm();
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: NEW_SECRET } });
    fireEvent.submit(screen.getByRole('form', { name: 'Add a new key' }));
    clickInDialog('Add key');

    const alert = await within(dialog()).findByRole('alert');
    expect(alert).toHaveTextContent('key •••• was rejected');
    expect(dialog()).not.toHaveTextContent(NEW_SECRET);
    expect(screen.getByLabelText('API key')).toHaveValue(NEW_SECRET);

    clickInDialog('Add key');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(addProviderKey).toHaveBeenCalledTimes(2);
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

  it('keeps the delete confirmation open with the error when deleting fails', async () => {
    vi.mocked(deleteProviderKey).mockRejectedValueOnce(new Error('in use'));
    render(<ProviderKeysTab />);

    fireEvent.click(await screen.findByRole('button', { name: `Delete key ${dbKey.key_prefix}` }));
    clickInDialog('Delete');

    expect(await within(dialog()).findByRole('alert')).toHaveTextContent('in use');
    expect(deleteProviderKey).toHaveBeenCalledTimes(1);
  });

  it('disables a DB key after confirmation', async () => {
    vi.mocked(setProviderKeyStatus).mockResolvedValue({
      id: 'key-1',
      provider: 'zai',
      status: 'disabled',
      pools_updated: 1,
    });
    render(<ProviderKeysTab />);

    fireEvent.click(await screen.findByRole('button', { name: `Disable key ${dbKey.key_prefix}` }));
    expect(screen.getByRole('dialog', { name: `Disable key ${dbKey.key_prefix}?` })).toBeVisible();
    expect(setProviderKeyStatus).not.toHaveBeenCalled();
    clickInDialog('Disable');

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(vi.mocked(setProviderKeyStatus).mock.calls).toEqual([['key-1', false]]);
    expect(toast.success).toHaveBeenCalledWith('Key disabled');
  });

  it('disables an env key after confirmation, and Cancel writes nothing', async () => {
    vi.mocked(disableProviderEnvKey).mockResolvedValue({
      id: 'env:abc',
      provider: 'zai',
      pools_updated: 1,
    });
    render(<ProviderKeysTab />);

    fireEvent.click(
      await screen.findByRole('button', { name: `Disable key ${envKey.key_prefix}` }),
    );
    const confirm = screen.getByRole('dialog', { name: `Disable env key ${envKey.key_prefix}?` });
    expect(confirm).toHaveTextContent('stops being used immediately');
    clickInDialog('Cancel');
    expect(disableProviderEnvKey).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: `Disable key ${envKey.key_prefix}` }));
    clickInDialog('Disable');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(vi.mocked(disableProviderEnvKey).mock.calls).toEqual([['zai', 'env:abc']]);
    expect(toast.success).toHaveBeenCalledWith('Env key disabled');
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it('re-enables a disabled env key after confirmation', async () => {
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
    expect(
      screen.getByRole('dialog', { name: `Enable env key ${envKey.key_prefix}?` }),
    ).toBeVisible();
    expect(enableProviderEnvKey).not.toHaveBeenCalled();
    clickInDialog('Enable');

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(vi.mocked(enableProviderEnvKey).mock.calls).toEqual([['zai', 'env:abc']]);
    expect(toast.error).toHaveBeenCalledWith(
      'Env key re-enabled but could not be re-added to any pool.',
    );
  });

  it('offers only the tier control for a reservation-only env row', async () => {
    // An active DB row holds the same credential, so this entry exists purely to
    // carry the reservation — the Disable endpoint rejects it by design.
    vi.mocked(listProviderKeys).mockResolvedValue({
      provider: 'zai',
      keys: [{ ...envKey, min_role: 'pro', reservation_only: true }],
    });
    await openTierEditor();

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

    // Read-only, the row shows what it declares...
    expect(
      await screen.findByLabelText(`Reserved tier for key ${dbKey.key_prefix}`),
    ).toHaveTextContent('shared');
    fireEvent.click(screen.getByRole('button', { name: 'Edit tiers zai key tiers' }));
    const select = screen.getByLabelText<HTMLSelectElement>(
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
