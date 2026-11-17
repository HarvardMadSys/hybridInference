// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import toast from 'react-hot-toast';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { UserTable } from './UserTable';
import type { UserRow } from './types';

/**
 * The "Single sign-on" block of the user detail panel: the identities that can
 * sign in to the account, and the one thing an administrator does to one --
 * unlink it, confirmed with the administrator's own password, or with a code
 * when the administrator has an authenticator app.
 */

// The signed-in administrator's own second factor, as the provider reports it.
const twoFactor = vi.hoisted(() => ({ status: undefined as { enabled: boolean } | undefined }));

vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }));
vi.mock('@/components/providers/TwoFactorProvider', () => ({
  useTwoFactor: () => ({ status: twoFactor.status, isPending: false, refresh: async () => {} }),
}));
vi.mock('@/lib/api/admin', () => ({
  getUserDetail: vi.fn(),
  listModelVisibility: vi.fn(),
  unlinkUserSsoIdentity: vi.fn(),
}));

import { getUserDetail, listModelVisibility, unlinkUserSsoIdentity } from '@/lib/api/admin';
import type { UserDetail, UserSsoIdentity } from '@/lib/api/admin';

const listed: UserRow = {
  id: 'user-1',
  email: 'carol@example.edu',
  user_name: 'Carol',
  role: 'free',
  status: 'active',
  email_verified: true,
  approval_note: null,
  reviewed_at: null,
  reviewed_by: null,
  signup_reason: null,
  admin_note: null,
  created_at: '2024-01-01T00:00:00Z',
  last_login_at: null,
  has_key: true,
  key_prefix: 'hyi_123',
  key_status: 'active',
  usage_today_usd: '0',
  usage_month_usd: '0',
  usage_alltime_usd: '0',
  usage_alltime_requests: 0,
  usage_alltime_tokens: 0,
};

function identity(extra: Partial<UserSsoIdentity> = {}): UserSsoIdentity {
  return {
    id: 'idn_1',
    provider_id: 'example-univ',
    provider_name: 'Example University',
    email: 'carol@example.edu',
    linked_at: '2026-11-01T12:00:00Z',
    last_login_at: null,
    ...extra,
  };
}

function detail(overrides: Partial<UserDetail> = {}): UserDetail {
  return {
    id: 'user-1',
    email: 'carol@example.edu',
    user_name: 'Carol',
    role: 'free',
    status: 'active',
    email_verified: true,
    created_at: '2024-01-01T00:00:00Z',
    last_login_at: null,
    has_key: true,
    key_prefix: 'hyi_123',
    quota_daily_usd: 100,
    quota_monthly_usd: null,
    usage_today_usd: 0,
    usage_today_requests: 0,
    usage_month_usd: 0,
    usage_month_requests: 0,
    models_used: [],
    disabled_models: [],
    last_request_at: null,
    max_concurrent_requests: null,
    admin_note: null,
    suspension_message: null,
    avg_turns: null,
    avg_user_turns: null,
    ask_question_fraction: null,
    ...overrides,
  };
}

function renderTable(user: UserRow = listed) {
  return render(
    <UserTable
      users={[user]}
      costHistories={{}}
      turnAverages={{}}
      askQuestionFractions={{}}
      automationScores={{}}
      scoreState="idle"
      scoreSortDir={null}
      onScoreHeader={vi.fn()}
      density="comfortable"
      filterState={{
        status: null,
        search: '',
        sortBy: 'created',
        minCostToday: null,
        minCostMonth: null,
        quotaState: null,
        provider: null,
        activeWithinHours: null,
        anomaly: null,
        view: null,
      }}
      onSortChange={vi.fn()}
      onApprove={vi.fn(async () => undefined)}
      onApproveMany={vi.fn(async () => undefined)}
      onReject={vi.fn(async () => undefined)}
      onUpdate={vi.fn(async () => undefined)}
      onSuspend={vi.fn(async () => undefined)}
      onResume={vi.fn(async () => undefined)}
      onDelete={vi.fn(async () => undefined)}
      onHardDelete={vi.fn(async () => undefined)}
    />,
  );
}

async function expand() {
  fireEvent.click(screen.getByText('carol@example.edu'));
  await screen.findByRole('textbox', { name: 'Admin note' });
}

const block = () => screen.queryByRole('region', { name: 'Single sign-on' });

beforeEach(() => {
  vi.clearAllMocks();
  twoFactor.status = undefined;
  vi.mocked(listModelVisibility).mockResolvedValue({ models: [] });
  vi.mocked(unlinkUserSsoIdentity).mockResolvedValue({ message: 'Unlinked.' });
});

afterEach(cleanup);

describe('the single sign-on block of the user detail panel', () => {
  it('is absent for an account with no identity, and for a gateway that sends none', async () => {
    vi.mocked(getUserDetail).mockResolvedValueOnce(detail({ sso_identities: [] }));
    renderTable();
    await expand();
    expect(block()).toBeNull();
    cleanup();

    // A gateway that predates single sign-on has no such key.
    vi.mocked(getUserDetail).mockResolvedValueOnce(detail());
    renderTable();
    await expand();
    expect(block()).toBeNull();
  });

  it('lists each identity with its provider, email, when it was connected and last used', async () => {
    vi.mocked(getUserDetail).mockResolvedValueOnce(
      detail({
        sso_identities: [
          identity(),
          identity({
            id: 'idn_2',
            provider_name: 'Example Labs',
            email: null,
            linked_at: '2026-10-02T12:00:00Z',
            last_login_at: new Date(Date.now() - 5 * 60_000).toISOString(),
          }),
        ],
      }),
    );
    renderTable();
    await expand();

    const rows = within(block()!).getAllByRole('listitem');
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent('Example University');
    expect(rows[0]).toHaveTextContent('carol@example.edu');
    expect(rows[0]).toHaveTextContent('Connected Nov 1, 2026');
    expect(rows[0]).toHaveTextContent('Last sign-in Never');
    expect(rows[1]).toHaveTextContent('Example Labs');
    expect(rows[1]).toHaveTextContent('Connected Oct 2, 2026');
    expect(rows[1]).toHaveTextContent('Last sign-in 5m ago');
    expect(within(rows[1]).getByRole('button', { name: 'Unlink Example Labs' })).toBeEnabled();
  });

  it('is shown for a suspended account too', async () => {
    vi.mocked(getUserDetail).mockResolvedValueOnce(
      detail({ status: 'suspended', sso_identities: [identity()] }),
    );
    renderTable({ ...listed, status: 'suspended' });
    await expand();

    expect(block()).toBeInTheDocument();
  });

  it('unlinks after the administrator’s password, then shows what is left', async () => {
    vi.mocked(getUserDetail)
      .mockResolvedValueOnce(detail({ sso_identities: [identity()] }))
      .mockResolvedValueOnce(detail({ sso_identities: [] }));
    renderTable();
    await expand();

    fireEvent.click(screen.getByRole('button', { name: 'Unlink Example University' }));

    const dialog = screen.getByRole('dialog', {
      name: 'Unlink Example University from carol@example.edu?',
    });
    // What it does, and what it leaves an account without a password with.
    expect(dialog).toHaveTextContent(
      'An account with no password will have no way to sign in until it resets its password.',
    );
    expect(dialog).toHaveTextContent('Sessions and API keys stay as they are');
    expect(unlinkUserSsoIdentity).not.toHaveBeenCalled();

    // The admin token alone is not enough.
    fireEvent.click(within(dialog).getByRole('button', { name: 'Unlink' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'Enter your password to unlink this identity.',
    );
    expect(unlinkUserSsoIdentity).not.toHaveBeenCalled();

    fireEvent.change(within(dialog).getByLabelText('Your password'), {
      target: { value: 'Admin-pw-1' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Unlink' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(vi.mocked(unlinkUserSsoIdentity).mock.calls).toEqual([
      ['user-1', 'idn_1', 'Admin-pw-1'],
    ]);
    // The detail is read again, and the block goes with its last identity.
    expect(getUserDetail).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(block()).toBeNull());
    expect(toast.success).toHaveBeenCalledWith('Single sign-on identity unlinked.');
  });

  it('asks an administrator who has an authenticator app for no password', async () => {
    twoFactor.status = { enabled: true };
    vi.mocked(getUserDetail)
      .mockResolvedValueOnce(detail({ sso_identities: [identity()] }))
      .mockResolvedValueOnce(detail({ sso_identities: [] }));
    renderTable();
    await expand();
    fireEvent.click(screen.getByRole('button', { name: 'Unlink Example University' }));
    const dialog = screen.getByRole('dialog');

    // The gateway takes a code from this account, and the console's own prompt asks for it.
    expect(dialog).toHaveTextContent('A code from your authenticator app confirms this.');
    expect(dialog).not.toHaveTextContent('Confirm with your password.');
    expect(within(dialog).queryByLabelText('Your password')).toBeNull();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Unlink' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(vi.mocked(unlinkUserSsoIdentity).mock.calls).toEqual([['user-1', 'idn_1', undefined]]);
    await waitFor(() => expect(block()).toBeNull());
  });

  it('keeps the confirmation open, with the reason, when the gateway refuses', async () => {
    vi.mocked(getUserDetail).mockResolvedValue(detail({ sso_identities: [identity()] }));
    vi.mocked(unlinkUserSsoIdentity)
      .mockRejectedValueOnce(new Error('Current password is incorrect.'))
      .mockResolvedValueOnce({ message: 'Unlinked.' });
    renderTable();
    await expand();
    fireEvent.click(screen.getByRole('button', { name: 'Unlink Example University' }));
    const dialog = screen.getByRole('dialog');

    fireEvent.change(within(dialog).getByLabelText('Your password'), { target: { value: 'no' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Unlink' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'Current password is incorrect.',
    );
    expect(block()).toBeInTheDocument();
    expect(toast.success).not.toHaveBeenCalled();

    // Corrected in place, without starting over.
    fireEvent.change(within(dialog).getByLabelText('Your password'), {
      target: { value: 'Admin-pw-1' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Unlink' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(unlinkUserSsoIdentity).toHaveBeenLastCalledWith('user-1', 'idn_1', 'Admin-pw-1');
  });

  it('writes nothing when the confirmation is cancelled', async () => {
    vi.mocked(getUserDetail).mockResolvedValueOnce(detail({ sso_identities: [identity()] }));
    renderTable();
    await expand();
    fireEvent.click(screen.getByRole('button', { name: 'Unlink Example University' }));

    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(unlinkUserSsoIdentity).not.toHaveBeenCalled();
    expect(block()).toBeInTheDocument();
  });

  it('drops the unlinked identity itself when the detail cannot be read again', async () => {
    vi.mocked(getUserDetail)
      .mockResolvedValueOnce(
        detail({
          sso_identities: [identity(), identity({ id: 'idn_2', provider_name: 'Example Labs' })],
        }),
      )
      .mockRejectedValueOnce(new Error('busy'));
    renderTable();
    await expand();
    fireEvent.click(screen.getByRole('button', { name: 'Unlink Example University' }));
    const dialog = screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Your password'), {
      target: { value: 'Admin-pw-1' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Unlink' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    // Unlinked on the gateway: a retry would only be told it is gone.
    const rows = within(block()!).getAllByRole('listitem');
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent('Example Labs');
  });
});
