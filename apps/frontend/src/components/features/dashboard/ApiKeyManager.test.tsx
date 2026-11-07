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
  useRevealApiKey: vi.fn(),
}));

let authUser: { has_password?: boolean } | null = { has_password: true };
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { isAuthenticated: true, loading: false, user: authUser } }),
}));

const push = vi.fn();
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push }),
}));

vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useBranding: () => ({ appName: 'Test App' }),
}));

vi.mock('react-hot-toast', () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

import toast from 'react-hot-toast';
import {
  useApiKeys,
  useCreateApiKey,
  useDeleteApiKey,
  useRegenerateApiKey,
  useRenameApiKey,
  useRevealApiKey,
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
  reveal: Mutation;
};

function mockKeys(items: ApiKeyListItem[]): void {
  vi.mocked(useApiKeys).mockReturnValue({
    data: { keys: items },
    isLoading: false,
    error: null,
  } as ReturnType<typeof useApiKeys>);
  mutations = {
    create: idle(),
    remove: idle(),
    regenerate: idle(),
    rename: idle(),
    reveal: {
      mutateAsync: vi.fn().mockResolvedValue({ key_prefix: 'hyi-testkey01', api_key: FULL_KEY }),
      isPending: false,
    },
  };
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
  vi.mocked(useRevealApiKey).mockReturnValue(
    mutations.reveal as unknown as ReturnType<typeof useRevealApiKey>,
  );
}

function mockList(item: ApiKeyListItem): void {
  mockKeys([item]);
}

const FULL_KEY = 'hyi-testkey01-FULL-SECRET-VALUE';
const MASKED = 'hyi-testkey01********************';

function makeItem(overrides: Partial<ApiKeyListItem> = {}): ApiKeyListItem {
  return {
    // The listing never carries the full key; the password reveals it.
    api_key: null,
    revealable: true,
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
  authUser = { has_password: true };
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

async function confirmPassword(password = 'my-password'): Promise<HTMLElement> {
  const dialog = screen.getByRole('dialog', { name: "Confirm it's you" });
  const field = within(dialog).getByLabelText('Password');
  expect(field).toHaveFocus();
  expect(field).toHaveAttribute('type', 'password');
  fireEvent.change(field, { target: { value: password } });
  fireEvent.submit(field.closest('form')!);
  return dialog;
}

describe('ApiKeyManager reveal + copy', () => {
  it.each(['active', 'revoked'])(
    'reveals a masked %s key only once the password confirms it',
    async (status) => {
      mockList(makeItem({ status }));
      render(<ApiKeyManager />);

      expect(screen.getByText(MASKED)).toBeInTheDocument();
      expect(screen.queryByText(FULL_KEY)).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole('button', { name: 'Show key' }));
      const dialog = screen.getByRole('dialog', { name: "Confirm it's you" });
      // Nothing is asked of the server until a password is typed.
      expect(within(dialog).getByRole('button', { name: 'Show key' })).toBeDisabled();
      expect(mutations.reveal.mutateAsync).not.toHaveBeenCalled();
      await confirmPassword();

      expect(await screen.findByText(FULL_KEY)).toBeInTheDocument();
      expect(mutations.reveal.mutateAsync).toHaveBeenCalledWith({
        keyPrefix: 'hyi-testkey01',
        password: 'my-password',
      });
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

      // Hidden and shown again on this page without asking again.
      fireEvent.click(screen.getByRole('button', { name: 'Hide key' }));
      expect(screen.getByText(MASKED)).toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: 'Show key' }));
      expect(screen.getByText(FULL_KEY)).toBeInTheDocument();
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      expect(mutations.reveal.mutateAsync).toHaveBeenCalledTimes(1);
    },
  );

  it('copies a saved key once the password confirms it, and then without asking', async () => {
    mockList(makeItem());
    render(<ApiKeyManager />);

    fireEvent.click(screen.getByRole('button', { name: 'Copy key' }));
    expect(writeText).not.toHaveBeenCalled();
    const dialog = screen.getByRole('dialog', { name: "Confirm it's you" });
    expect(within(dialog).getByRole('button', { name: 'Copy key' })).toBeInTheDocument();
    await confirmPassword();

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(FULL_KEY));
    // Copying does not put the key on the screen.
    expect(screen.queryByText(FULL_KEY)).not.toBeInTheDocument();
    await waitFor(() => expect(toast.success).toHaveBeenCalled());

    fireEvent.click(screen.getByRole('button', { name: 'Copy key' }));
    expect(writeText).toHaveBeenCalledTimes(2);
    expect(mutations.reveal.mutateAsync).toHaveBeenCalledTimes(1);
  });

  it('keeps the key hidden and says why when the password is wrong', async () => {
    mockList(makeItem());
    mutations.reveal.mutateAsync.mockRejectedValueOnce(new Error('Current password is incorrect.'));
    render(<ApiKeyManager />);

    fireEvent.click(screen.getByRole('button', { name: 'Show key' }));
    const dialog = await confirmPassword('wrong');

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'Current password is incorrect.',
    );
    expect(screen.queryByText(FULL_KEY)).not.toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('reveals nothing when the password prompt is cancelled', () => {
    mockList(makeItem());
    render(<ApiKeyManager />);

    fireEvent.click(screen.getByRole('button', { name: 'Show key' }));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mutations.reveal.mutateAsync).not.toHaveBeenCalled();
    expect(screen.getByText(MASKED)).toBeInTheDocument();
  });

  it('says so when the clipboard refuses the key', async () => {
    mockList(makeItem({ api_key: FULL_KEY }));
    writeText.mockRejectedValueOnce(new Error('denied'));
    render(<ApiKeyManager />);

    fireEvent.click(screen.getByRole('button', { name: 'Copy key' }));

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('still shows a full key that an older gateway listed', () => {
    mockList(makeItem({ api_key: FULL_KEY, revealable: undefined }));
    render(<ApiKeyManager />);

    fireEvent.click(screen.getByRole('button', { name: 'Show key' }));

    expect(screen.getByText(FULL_KEY)).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('does not render reveal/copy controls for a legacy key without a saved value', () => {
    mockList(makeItem({ revealable: false }));
    render(<ApiKeyManager />);

    expect(screen.getByText(MASKED)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Show key' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Copy key' })).not.toBeInTheDocument();
    expect(
      screen.getByText(/Full key is only shown immediately after creation or regeneration/),
    ).toBeInTheDocument();
  });

  it('tells an account without a password how to get a key instead', () => {
    authUser = { has_password: false };
    mockList(makeItem());
    render(<ApiKeyManager />);

    expect(screen.queryByRole('button', { name: 'Show key' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Copy key' })).not.toBeInTheDocument();
    expect(screen.getByText(/Showing a saved key needs your password/)).toBeInTheDocument();
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
    expect(screen.getByRole('menuitem', { name: 'Limits' })).toHaveFocus();
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

  it('links each key to its own page and sums up its limits', () => {
    mockKeys([
      makeItem({
        key_prefix: 'hyi-aaaaaaaa',
        name: 'ci',
        budget_daily_usd: 5,
        allowed_models: ['m-a', 'm-b'],
      }),
      makeItem({ key_prefix: 'hyi-bbbbbbbb', name: 'laptop' }),
    ]);
    render(<ApiKeyManager />);

    expect(screen.getByRole('link', { name: 'ci' })).toHaveAttribute(
      'href',
      '/dashboard/keys/hyi-aaaaaaaa',
    );
    expect(screen.getByText('$5.00/day · 2 models')).toBeInTheDocument();
    expect(screen.getByText('No limits')).toBeInTheDocument();
  });

  it("opens a key's limits from its actions", () => {
    mockKeys([makeItem({ key_prefix: 'hyi-aaaaaaaa', name: 'ci' })]);
    render(<ApiKeyManager />);

    fireEvent.click(screen.getByRole('button', { name: 'Actions for ci' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Limits' }));

    expect(push).toHaveBeenCalledWith('/dashboard/keys/hyi-aaaaaaaa');
  });

  it("says that regenerating keeps the key's name, limits and spend", () => {
    mockKeys([makeItem({ key_prefix: 'hyi-aaaaaaaa', name: 'ci' })]);
    render(<ApiKeyManager />);

    fireEvent.click(screen.getByRole('button', { name: 'Actions for ci' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Regenerate' }));

    expect(
      within(screen.getByRole('dialog')).getByText(/keeps its name, limits and spend/),
    ).toBeInTheDocument();
  });
});
