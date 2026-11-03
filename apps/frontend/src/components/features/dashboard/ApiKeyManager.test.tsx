// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ApiKeyListItem } from '@/lib/api/user';
import { ApiKeyManager } from './ApiKeyManager';

vi.mock('@/lib/hooks', () => ({
  useApiKeys: vi.fn(),
  useCreateApiKey: vi.fn(),
  useDeleteApiKey: vi.fn(),
  useRegenerateApiKey: vi.fn(),
  useRenameApiKey: vi.fn(),
}));

vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useBranding: () => ({ appName: 'Test App' }),
}));

vi.mock('react-hot-toast', () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

import {
  useApiKeys,
  useCreateApiKey,
  useDeleteApiKey,
  useRegenerateApiKey,
  useRenameApiKey,
} from '@/lib/hooks';

type Mutation = { mutateAsync: ReturnType<typeof vi.fn>; isPending: boolean };

function idle(): Mutation {
  return { mutateAsync: vi.fn().mockResolvedValue({ api_key: 'hyi-new' }), isPending: false };
}

let mutations: {
  create: Mutation;
  remove: Mutation;
  regenerate: Mutation;
  rename: Mutation;
};

function mockKeys(items: ApiKeyListItem[]): void {
  vi.mocked(useApiKeys).mockReturnValue({
    data: { keys: items },
    isLoading: false,
    error: null,
  } as ReturnType<typeof useApiKeys>);
  mutations = { create: idle(), remove: idle(), regenerate: idle(), rename: idle() };
  vi.mocked(useCreateApiKey).mockReturnValue(
    mutations.create as unknown as ReturnType<typeof useCreateApiKey>,
  );
  vi.mocked(useDeleteApiKey).mockReturnValue(
    mutations.remove as unknown as ReturnType<typeof useDeleteApiKey>,
  );
  vi.mocked(useRegenerateApiKey).mockReturnValue(
    mutations.regenerate as unknown as ReturnType<typeof useRegenerateApiKey>,
  );
  vi.mocked(useRenameApiKey).mockReturnValue(
    mutations.rename as unknown as ReturnType<typeof useRenameApiKey>,
  );
}

function mockList(item: ApiKeyListItem): void {
  mockKeys([item]);
}

const FULL_KEY = 'hyi-testkey01-FULL-SECRET-VALUE';
const MASKED = 'hyi-testkey01********************';

function makeItem(overrides: Partial<ApiKeyListItem> = {}): ApiKeyListItem {
  return {
    api_key: FULL_KEY,
    key_prefix: 'hyi-testkey01',
    key_masked: MASKED,
    created_at: '2026-05-06T12:00:00.000Z',
    last_used_at: null,
    status: 'active',
    ...overrides,
  };
}

let writeText: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.spyOn(window, 'prompt').mockImplementation(() => {
    throw new Error('prompt() is not supported.');
  });
  vi.spyOn(window, 'confirm').mockImplementation(() => {
    throw new Error('confirm() is not supported.');
  });
  writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText },
    configurable: true,
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  // window.prompt / window.confirm spies must not leak into the next test.
  vi.restoreAllMocks();
});

describe('ApiKeyManager reveal + copy', () => {
  it.each(['active', 'revoked'])('masks a %s key and reveals its saved value', (status) => {
    mockList(makeItem({ status }));
    render(<ApiKeyManager />);

    // Masked value is shown; the full key is hidden until the user reveals it.
    expect(screen.getByText(MASKED)).toBeInTheDocument();
    expect(screen.queryByText(FULL_KEY)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Show key' }));

    // Full key is now visible and the toggle flips to "Hide key".
    expect(screen.getByText(FULL_KEY)).toBeInTheDocument();
    expect(screen.queryByText(MASKED)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Hide key' })).toBeInTheDocument();
  });

  it.each(['active', 'revoked'])('copies the full %s key to the clipboard', (status) => {
    mockList(makeItem({ status }));
    render(<ApiKeyManager />);

    fireEvent.click(screen.getByRole('button', { name: 'Copy key' }));

    expect(writeText).toHaveBeenCalledWith(FULL_KEY);
  });

  it('does not render reveal/copy controls for a legacy key without api_key', () => {
    mockList(makeItem({ api_key: null }));
    render(<ApiKeyManager />);

    expect(screen.getByText(MASKED)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Show key' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Copy key' })).not.toBeInTheDocument();
    expect(
      screen.getByText(/Full key is only shown immediately after creation or regeneration/),
    ).toBeInTheDocument();
  });
});

describe('ApiKeyManager with several keys', () => {
  it('offers Create while an account holds keys, and names each one', () => {
    mockKeys([
      makeItem({ key_prefix: 'hyi-aaaaaaaa', key_masked: 'hyi-aaaa****', name: 'laptop' }),
      makeItem({ key_prefix: 'hyi-bbbbbbbb', key_masked: 'hyi-bbbb****', name: null }),
    ]);
    render(<ApiKeyManager />);

    expect(screen.getByRole('button', { name: 'Create API Key' })).toBeEnabled();
    expect(screen.getByText('laptop')).toBeInTheDocument();
    expect(screen.getByText('Unnamed key')).toBeInTheDocument();
    // No limit is presented: the server's ceiling is a safety net, not a quota.
    expect(screen.queryByText(/ of \d+ active/)).not.toBeInTheDocument();
  });

  it('shows what each key spent today and this month', () => {
    mockKeys([
      makeItem({
        key_prefix: 'hyi-aaaaaaaa',
        key_masked: 'hyi-aaaa****',
        spent_today_usd: 0.5,
        spent_month_usd: 12.25,
      }),
    ]);
    render(<ApiKeyManager />);

    expect(screen.getByText('$0.50 today')).toBeInTheDocument();
    expect(screen.getByText('$12.25 this month')).toBeInTheDocument();
  });

  it('creates a key with an in-page form when native prompts are unavailable', async () => {
    mockKeys([]);
    render(<ApiKeyManager />);

    fireEvent.click(screen.getByRole('button', { name: 'Create API Key' }));

    const dialog = screen.getByRole('dialog', { name: 'Create API Key' });
    const input = within(dialog).getByRole('textbox', { name: 'Name (optional)' });
    expect(input).toHaveFocus();
    fireEvent.change(input, { target: { value: '  ci  ' } });
    fireEvent.submit(input.closest('form')!);
    await waitFor(() => expect(mutations.create.mutateAsync).toHaveBeenCalledWith('ci'));
    expect(window.prompt).not.toHaveBeenCalled();
    expect(await screen.findByText('hyi-new')).toBeInTheDocument();
  });

  it('cancels without creating a key and restores focus to the trigger', () => {
    mockKeys([]);
    render(<ApiKeyManager />);

    const trigger = screen.getByRole('button', { name: 'Create API Key' });
    trigger.focus();
    fireEvent.click(trigger);

    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Create API Key' })).toHaveFocus();
    expect(mutations.create.mutateAsync).not.toHaveBeenCalled();
  });

  it('supports keyboard row actions and returns focus after cancelling a dialog', () => {
    mockKeys([makeItem({ name: 'laptop' })]);
    render(<ApiKeyManager />);
    const trigger = screen.getByRole('button', { name: 'Actions for laptop' });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: 'ArrowDown' });
    expect(screen.getByRole('menuitem', { name: 'Rename' })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'End' });
    expect(screen.getByRole('menuitem', { name: 'Revoke' })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole('menuitem', { name: 'Rename' }));
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(screen.getByRole('textbox')).toHaveFocus();
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(mutations.rename.mutateAsync).not.toHaveBeenCalled();
  });

  it('regenerates the key on its own row', async () => {
    mockKeys([
      makeItem({ key_prefix: 'hyi-aaaaaaaa', key_masked: 'hyi-aaaa****' }),
      makeItem({ key_prefix: 'hyi-bbbbbbbb', key_masked: 'hyi-bbbb****' }),
    ]);
    render(<ApiKeyManager />);

    fireEvent.click(screen.getByRole('button', { name: 'Actions for hyi-bbbbbbbb' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Regenerate' }));
    const dialog = screen.getByRole('dialog', { name: 'Regenerate' });
    expect(within(dialog).getByText('hyi-bbbb****')).toBeInTheDocument();
    expect(mutations.regenerate.mutateAsync).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Regenerate' }));

    await waitFor(() =>
      expect(mutations.regenerate.mutateAsync).toHaveBeenCalledWith('hyi-bbbbbbbb'),
    );
  });

  it('renames a key, clearing the name on a blank answer', async () => {
    mockKeys([makeItem({ key_prefix: 'hyi-aaaaaaaa', name: 'old' })]);
    render(<ApiKeyManager />);

    fireEvent.click(screen.getByRole('button', { name: 'Actions for old' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Rename' }));
    const dialog = screen.getByRole('dialog', { name: 'Rename' });
    const input = within(dialog).getByRole('textbox');
    expect(input).toHaveValue('old');
    fireEvent.change(input, { target: { value: '   ' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Rename' }));

    await waitFor(() =>
      expect(mutations.rename.mutateAsync).toHaveBeenCalledWith({
        keyPrefix: 'hyi-aaaaaaaa',
        name: null,
      }),
    );
  });

  it('keeps a failed create open with its name so it can be retried', async () => {
    mockKeys([]);
    mutations.create.mutateAsync.mockRejectedValueOnce(new Error('Could not create this key'));
    render(<ApiKeyManager />);
    fireEvent.click(screen.getByRole('button', { name: 'Create API Key' }));
    const dialog = screen.getByRole('dialog');
    const input = within(dialog).getByRole('textbox');
    fireEvent.change(input, { target: { value: 'my laptop' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create API Key' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not create this key');
    expect(input).toHaveValue('my laptop');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create API Key' }));
    expect(await screen.findByText('hyi-new')).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('explains revocation and waits for confirmation before invalidating a key', async () => {
    mockKeys([makeItem({ name: 'laptop' })]);
    render(<ApiKeyManager />);
    fireEvent.click(screen.getByRole('button', { name: 'Actions for laptop' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Revoke' }));
    const dialog = screen.getByRole('dialog', { name: 'Revoke' });
    expect(within(dialog).getByText(/stop working immediately/)).toBeInTheDocument();
    expect(mutations.remove.mutateAsync).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Revoke' }));
    await waitFor(() => expect(mutations.remove.mutateAsync).toHaveBeenCalledWith('hyi-testkey01'));
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it('shows revoked keys with their usage history directly after active keys', () => {
    mockKeys([
      makeItem({ key_prefix: 'hyi-aaaaaaaa', key_masked: 'hyi-live****' }),
      makeItem({
        key_prefix: 'hyi-dddddddd',
        key_masked: 'hyi-dead****',
        status: 'revoked',
        api_key: null,
      }),
    ]);
    render(<ApiKeyManager />);

    expect(screen.getByText('hyi-live****')).toBeInTheDocument();
    expect(screen.getByText('hyi-dead****')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Actions for hyi-dddddddd' }));
    expect(screen.getByRole('menuitem', { name: 'Remove' })).toBeInTheDocument();
    expect(screen.getByText('revoked')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /revoked keys/i })).not.toBeInTheDocument();
  });
});
