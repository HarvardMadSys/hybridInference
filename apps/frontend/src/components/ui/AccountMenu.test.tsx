// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AccountMenu } from './AccountMenu';

const push = vi.fn();
const replace = vi.fn();
const switchAccount = vi.fn();
const logout = vi.fn();
const logoutAll = vi.fn();

type Account = {
  id: string;
  email: string;
  user_name: string;
  role: string;
  active: boolean;
  signed_in: boolean;
};

let accounts: Account[] = [];
let maxAccounts = 5;

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push, replace }),
  usePathname: () => '/dashboard/settings',
}));

vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: {
      isAuthenticated: true,
      loading: false,
      user: { id: 'ada', email: 'ada@example.test', user_name: 'Ada', role: 'admin' },
    },
    accounts,
    maxAccounts,
    switchAccount,
    logout,
    logoutAll,
  }),
}));

function account(id: string, extra: Partial<Account> = {}): Account {
  return {
    id,
    email: `${id}@example.test`,
    user_name: id[0].toUpperCase() + id.slice(1),
    role: 'free',
    active: false,
    signed_in: true,
    ...extra,
  };
}

function openMenu() {
  fireEvent.click(screen.getByRole('button', { name: 'Ada' }));
}

beforeEach(() => {
  vi.clearAllMocks();
  accounts = [account('ada', { active: true, role: 'admin' }), account('bob')];
  maxAccounts = 5;
  switchAccount.mockResolvedValue(undefined);
  logout.mockResolvedValue(undefined);
  logoutAll.mockResolvedValue(undefined);
});

afterEach(() => cleanup());

describe('AccountMenu', () => {
  it('shows the current account and the browser’s others', () => {
    render(<AccountMenu />);
    openMenu();

    expect(screen.getByRole('menu')).toHaveTextContent('ada@example.test · admin');
    expect(screen.getByRole('menuitem', { name: /Bob/ })).toBeInTheDocument();
  });

  it('switches to another signed-in account', async () => {
    render(<AccountMenu />);
    openMenu();

    fireEvent.click(screen.getByRole('menuitem', { name: /Bob/ }));

    await waitFor(() => expect(switchAccount).toHaveBeenCalledWith('bob'));
  });

  it('sends a signed-out account to sign in again, returning here', () => {
    accounts = [account('ada', { active: true }), account('bob', { signed_in: false })];
    render(<AccountMenu />);
    openMenu();

    fireEvent.click(screen.getByRole('menuitem', { name: /Bob/ }));

    expect(switchAccount).not.toHaveBeenCalled();
    expect(push).toHaveBeenCalledWith(
      `/login?add=1&next=${encodeURIComponent('/dashboard/settings')}&identifier=${encodeURIComponent('bob@example.test')}`,
    );
  });

  it('adds another account through the sign-in form', () => {
    render(<AccountMenu />);
    openMenu();

    fireEvent.click(screen.getByRole('menuitem', { name: 'Add another account' }));

    expect(push).toHaveBeenCalledWith(
      `/login?add=1&next=${encodeURIComponent('/dashboard/settings')}`,
    );
  });

  it('says why it cannot add a sixth', () => {
    accounts = ['ada', 'bob', 'cy', 'di', 'ed'].map((id) => account(id));
    render(<AccountMenu />);
    openMenu();

    expect(screen.queryByRole('menuitem', { name: 'Add another account' })).toBeNull();
    expect(screen.getByRole('menu')).toHaveTextContent('the most it can');
  });

  it('logs out to the chooser while other accounts remain', async () => {
    render(<AccountMenu />);
    openMenu();

    fireEvent.click(screen.getByRole('menuitem', { name: 'Log out' }));

    await waitFor(() => expect(replace).toHaveBeenCalledWith('/login'));
    expect(logout).toHaveBeenCalledTimes(1);
  });

  it('logs out to the home page with the last account', async () => {
    accounts = [account('ada', { active: true })];
    render(<AccountMenu />);
    openMenu();

    expect(screen.queryByRole('menuitem', { name: 'Log out of all accounts' })).toBeNull();
    fireEvent.click(screen.getByRole('menuitem', { name: 'Log out' }));

    await waitFor(() => expect(replace).toHaveBeenCalledWith('/'));
  });

  it('logs every account out', async () => {
    render(<AccountMenu />);
    openMenu();

    fireEvent.click(screen.getByRole('menuitem', { name: 'Log out of all accounts' }));

    await waitFor(() => expect(logoutAll).toHaveBeenCalledTimes(1));
    expect(replace).toHaveBeenCalledWith('/');
  });

  it('closes on Escape', () => {
    render(<AccountMenu />);
    openMenu();

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(screen.queryByRole('menu')).toBeNull();
  });
});
