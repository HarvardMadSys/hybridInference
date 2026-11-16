// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useRef, type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { TwoFactorChallenge } from '@/lib/api/client';
import type { TwoFactorStatus } from '@/lib/api/twoFactor';
import { APIError } from '@/lib/utils/errors';

const api = vi.hoisted(() => ({
  status: vi.fn(),
  setup: vi.fn(),
  cancel: vi.fn(),
  confirm: vi.fn(),
  verify: vi.fn(),
  codes: vi.fn(),
  remove: vi.fn(),
}));
vi.mock('@/lib/api/twoFactor', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/twoFactor')>()),
  getTwoFactorStatus: api.status,
  startTwoFactorSetup: api.setup,
  cancelTwoFactorSetup: api.cancel,
  confirmTwoFactor: api.confirm,
  verifyTwoFactor: api.verify,
  newRecoveryCodes: api.codes,
  removeTwoFactor: api.remove,
}));

// What the prompt installs into the API client: the tests play the client.
let challenger: ((challenge: TwoFactorChallenge) => Promise<boolean>) | null = null;
vi.mock('@/lib/api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/client')>()),
  setTwoFactorChallenger: (next: typeof challenger) => {
    challenger = next;
  },
}));

let user: { id: string; is_admin: boolean; has_password?: boolean } | null;
const auth = () => ({ state: { loading: false, isAuthenticated: user !== null, user } });
vi.mock('@/components/providers/AuthProvider', () => ({ useAuth: () => auth() }));
vi.mock('@/components/providers', () => ({ useAuth: () => auth() }));
vi.mock('@/components/providers/useT', () => ({
  useT: () => (_key: string, fallback: string) => fallback,
}));

import { ReauthPassword } from '@/components/features/admin/confirm/ReauthPassword';
import { TwoFactorProvider } from '@/components/providers/TwoFactorProvider';
import { AdminTwoFactorGate } from './AdminTwoFactorGate';
import { TwoFactorSettings, useShowTwoFactorSettings } from './TwoFactorSettings';

const OFF: TwoFactorStatus = {
  enabled: false,
  pending: false,
  required: false,
  session_verified: false,
  recovery_codes_left: 0,
};
const ON: TwoFactorStatus = {
  ...OFF,
  enabled: true,
  session_verified: true,
  recovery_codes_left: 10,
};
const SECRET = 'JBSWY3DPEHPK3PXP';
const SETUP = { secret: SECRET, otpauth_uri: `otpauth://totp/Console:admin?secret=${SECRET}` };
const RECOVERY = ['AAAAA-11111', 'BBBBB-22222'];

function mount(ui: ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <TwoFactorProvider>{ui}</TwoFactorProvider>
    </QueryClientProvider>,
  );
}

const codeField = () => screen.getByLabelText(/Authentication code/);
const type = (field: HTMLElement, value: string) => fireEvent.change(field, { target: { value } });

beforeEach(() => {
  Object.values(api).forEach((mock) => mock.mockReset());
  api.status.mockResolvedValue(OFF);
  api.cancel.mockResolvedValue(undefined);
  user = { id: 'admin-1', is_admin: true, has_password: true };
  challenger = null;
  window.location.hash = '';
});
afterEach(cleanup);

describe('the code prompt', () => {
  // Wrapped: an async function that returned the promise would wait for it.
  async function refused(reason = 'unverified') {
    mount(null);
    let outcome: Promise<boolean> = Promise.resolve(false);
    await act(async () => {
      outcome = challenger!({ reason });
    });
    return { outcome };
  }

  it('asks for a code and lets the request go on once the gateway takes it', async () => {
    api.verify.mockResolvedValue({ verified: true, recovery_codes_left: 10 });
    const { outcome } = await refused('stale');

    expect(screen.getByRole('dialog')).toHaveTextContent('needs a code from the last few minutes');
    expect(codeField()).toHaveFocus();
    expect(screen.getByRole('button', { name: 'Verify' })).toBeDisabled();
    type(codeField(), ' 123456 ');
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));

    await expect(outcome).resolves.toBe(true);
    expect(api.verify).toHaveBeenCalledExactlyOnceWith('123456');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('keeps asking after a wrong code, and hands the refusal back on Cancel', async () => {
    api.verify.mockRejectedValue(new Error('That code is not valid.'));
    const { outcome } = await refused();

    type(codeField(), '000000');
    fireEvent.keyDown(codeField(), { key: 'Enter' });

    expect(await screen.findByRole('alert')).toHaveTextContent('That code is not valid.');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await expect(outcome).resolves.toBe(false);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('answers requests refused together with one dialog and one outcome', async () => {
    api.verify.mockResolvedValue({ verified: true, recovery_codes_left: 9 });
    mount(null);
    let first: Promise<boolean> = Promise.resolve(false);
    let second: Promise<boolean> = Promise.resolve(false);
    await act(async () => {
      first = challenger!({ reason: 'unverified' });
      second = challenger!({ reason: 'unverified' });
    });

    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    // A recovery code stands in for the device.
    type(codeField(), 'AAAAA-11111');
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));

    await expect(Promise.all([first, second])).resolves.toEqual([true, true]);
    expect(api.verify).toHaveBeenCalledExactlyOnceWith('AAAAA-11111');
  });

  it('says when the session turned up in another browser', async () => {
    await refused('environment_changed');
    expect(screen.getByRole('dialog')).toHaveTextContent('verified in a different browser or app');
  });
});

describe('administration behind the second factor', () => {
  const gated = () =>
    mount(
      <AdminTwoFactorGate>
        <p>the admin pages</p>
      </AdminTwoFactorGate>,
    );

  it('opens as before where the deployment does not require one', async () => {
    gated();
    expect(await screen.findByText('the admin pages')).toBeInTheDocument();
  });

  it('holds the pages back until it knows, so they do not load into refusals', async () => {
    let answer: (status: TwoFactorStatus) => void = () => {};
    api.status.mockReturnValue(new Promise<TwoFactorStatus>((resolve) => (answer = resolve)));
    gated();

    await waitFor(() => expect(api.status).toHaveBeenCalled());
    expect(screen.queryByText('the admin pages')).not.toBeInTheDocument();
    answer(OFF);
    expect(await screen.findByText('the admin pages')).toBeInTheDocument();
  });

  it('sends an administrator without an authenticator to Settings', async () => {
    api.status.mockResolvedValue({ ...OFF, required: true });
    gated();

    const link = await screen.findByRole('link', { name: 'Add an authenticator app' });
    expect(link).toHaveAttribute('href', '/dashboard/settings#two-factor');
    expect(screen.queryByText('the admin pages')).not.toBeInTheDocument();
  });

  it('asks a session that owes a code, then opens', async () => {
    api.status.mockResolvedValue({ ...ON, required: true, session_verified: false });
    api.verify.mockImplementation(async () => {
      api.status.mockResolvedValue({ ...ON, required: true });
      return { verified: true, recovery_codes_left: 10 };
    });
    gated();

    type(await screen.findByLabelText(/Authentication code/), '123456');
    expect(screen.queryByText('the admin pages')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));

    expect(await screen.findByText('the admin pages')).toBeInTheDocument();
    expect(api.verify).toHaveBeenCalledExactlyOnceWith('123456');
  });

  it('opens when the gateway has no such route, leaving the decision to it', async () => {
    api.status.mockRejectedValue(new Error('HTTP 404: Not Found'));
    gated();
    expect(await screen.findByText('the admin pages')).toBeInTheDocument();
  });
});

describe('the settings section', () => {
  function Section() {
    return useShowTwoFactorSettings() ? <TwoFactorSettings /> : <p>no section</p>;
  }

  it('takes an administrator from a password to an authenticator and its recovery codes', async () => {
    api.status.mockResolvedValue({ ...OFF, required: true });
    api.setup.mockResolvedValue(SETUP);
    api.confirm.mockImplementation(async () => {
      api.status.mockResolvedValue({ ...ON, required: true });
      return { recovery_codes: RECOVERY };
    });
    mount(<Section />);

    expect(await screen.findByText(/Administrator access is off for this account/)).toBeVisible();
    const add = screen.getByRole('button', { name: 'Add authenticator app' });
    expect(add).toBeDisabled();
    type(screen.getByLabelText('Your password'), 'Hunter2hunter2');
    fireEvent.click(add);

    expect(await screen.findByTitle('QR code for your authenticator app')).toBeInTheDocument();
    expect(api.setup).toHaveBeenCalledExactlyOnceWith('Hunter2hunter2');
    expect(screen.getByText(SECRET)).toBeInTheDocument();
    type(codeField(), '123 456');
    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }));

    const list = await screen.findByRole('list', { name: 'Recovery codes' });
    expect(api.confirm).toHaveBeenCalledExactlyOnceWith('123 456');
    expect(list).toHaveTextContent('AAAAA-11111');
    expect(list).toHaveTextContent('BBBBB-22222');
    fireEvent.click(screen.getByRole('button', { name: 'I have saved them' }));

    expect(await screen.findByText(/Recovery codes left: 10/)).toBeInTheDocument();
    expect(screen.queryByText('AAAAA-11111')).not.toBeInTheDocument();
    // Required of this administrator, so there is no turning it off.
    expect(screen.queryByRole('button', { name: 'Turn off' })).not.toBeInTheDocument();
  });

  it('says why when the password is wrong, and shows no secret', async () => {
    api.setup.mockRejectedValue(new Error('Current password is incorrect'));
    mount(<Section />);

    type(await screen.findByLabelText('Your password'), 'wrong');
    fireEvent.click(screen.getByRole('button', { name: 'Add authenticator app' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Current password is incorrect');
    expect(screen.queryByTitle('QR code for your authenticator app')).not.toBeInTheDocument();
  });

  it('tells the gateway when a shown secret is not going to be used', async () => {
    api.setup.mockResolvedValue(SETUP);
    mount(<Section />);
    type(await screen.findByLabelText('Your password'), 'Hunter2hunter2');
    fireEvent.click(screen.getByRole('button', { name: 'Add authenticator app' }));
    await screen.findByText(SECRET);

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(
      await screen.findByRole('button', { name: 'Add authenticator app' }),
    ).toBeInTheDocument();
    expect(screen.queryByText(SECRET)).not.toBeInTheDocument();
    expect(api.cancel).toHaveBeenCalledTimes(1);
  });

  it('goes back to the start when a setup ran out of time or of tries', async () => {
    api.setup.mockResolvedValue(SETUP);
    api.confirm.mockRejectedValue(
      new APIError('HTTP_409', 'It ran out of time or of tries. Start again.', 409),
    );
    mount(<Section />);
    type(await screen.findByLabelText('Your password'), 'Hunter2hunter2');
    fireEvent.click(screen.getByRole('button', { name: 'Add authenticator app' }));
    type(await screen.findByLabelText(/Authentication code/), '123456');
    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Start again.');
    // The old secret is worthless now, so it is not left on the page.
    expect(screen.queryByText(SECRET)).not.toBeInTheDocument();
    expect(screen.getByLabelText('Your password')).toBeInTheDocument();
    // Nothing to cancel: the gateway already dropped it.
    expect(api.cancel).not.toHaveBeenCalled();
  });

  it('changes the app and makes new recovery codes without the password', async () => {
    api.status.mockResolvedValue({ ...ON, recovery_codes_left: 2 });
    api.codes.mockResolvedValue({ recovery_codes: RECOVERY });
    api.setup.mockResolvedValue(SETUP);
    mount(<Section />);

    expect(await screen.findByText(/Few recovery codes are left/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'New recovery codes' }));
    expect(await screen.findByRole('list', { name: 'Recovery codes' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'I have saved them' }));

    fireEvent.click(await screen.findByRole('button', { name: 'Change authenticator app' }));
    expect(await screen.findByTitle('QR code for your authenticator app')).toBeInTheDocument();
    expect(api.setup).toHaveBeenCalledExactlyOnceWith(undefined);
  });

  it('turns off, after asking, for an account it is not required of', async () => {
    api.status.mockResolvedValue(ON);
    api.remove.mockImplementation(async () => {
      api.status.mockResolvedValue(OFF);
      return OFF;
    });
    mount(<Section />);

    fireEvent.click(await screen.findByRole('button', { name: 'Turn off' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('every recovery code stop working');
    expect(api.remove).not.toHaveBeenCalled();
    fireEvent.click(screen.getAllByRole('button', { name: 'Turn off' })[1]);

    expect(
      await screen.findByRole('button', { name: 'Add authenticator app' }),
    ).toBeInTheDocument();
    expect(api.remove).toHaveBeenCalledTimes(1);
  });

  it('is an administrator’s: another account sees it only with an authenticator or by its link', async () => {
    user = { id: 'user-1', is_admin: false, has_password: true };
    const { unmount } = mount(<Section />);
    await waitFor(() => expect(api.status).toHaveBeenCalled());
    expect(screen.getByText('no section')).toBeInTheDocument();
    unmount();

    window.location.hash = '#two-factor';
    mount(<Section />);
    expect(
      await screen.findByRole('button', { name: 'Add authenticator app' }),
    ).toBeInTheDocument();
  });

  it('tells an account without a password to set one first', async () => {
    user = { id: 'admin-1', is_admin: true, has_password: false };
    mount(<Section />);
    expect(await screen.findByText(/Set a password first/)).toBeInTheDocument();
    expect(screen.queryByLabelText('Your password')).not.toBeInTheDocument();
  });
});

describe('confirming a guarded admin change', () => {
  function Field() {
    return <ReauthPassword valueRef={useRef('')} label="Your password" />;
  }

  it('asks for the password of an administrator without an authenticator', async () => {
    mount(<Field />);
    await waitFor(() => expect(api.status).toHaveBeenCalled());
    expect(screen.getByLabelText('Your password')).toBeInTheDocument();
  });

  it('asks nothing of one with an authenticator: the code prompt confirms', async () => {
    api.status.mockResolvedValue(ON);
    mount(<Field />);
    expect(
      await screen.findByText(/A code from your authenticator app confirms this/),
    ).toBeVisible();
    expect(screen.queryByLabelText('Your password')).not.toBeInTheDocument();
  });

  it('keeps the password field where there is no provider to ask', () => {
    render(<Field />);
    expect(screen.getByLabelText('Your password')).toBeInTheDocument();
    expect(api.status).not.toHaveBeenCalled();
  });
});

describe('reading the status', () => {
  it('is left alone on pages that never ask, and for a visitor', async () => {
    mount(<p>a page</p>);
    await act(async () => {});
    expect(api.status).not.toHaveBeenCalled();
    cleanup();

    user = null;
    mount(
      <AdminTwoFactorGate>
        <p>the admin pages</p>
      </AdminTwoFactorGate>,
    );
    await act(async () => {});
    expect(api.status).not.toHaveBeenCalled();
  });
});
