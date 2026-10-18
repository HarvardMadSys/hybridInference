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
const chooseLocale = vi.fn();
const setTheme = vi.fn();
let selectedTheme = 'system';

vi.mock('@/components/providers/localeChoice', () => ({ useChooseLocale: () => chooseLocale }));
vi.mock('@/components/providers/ThemeProvider', () => ({
  useTheme: () => ({ theme: selectedTheme, resolvedTheme: 'light', setTheme }),
}));

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
  selectedTheme = 'system';
  chooseLocale.mockResolvedValue(undefined);
  switchAccount.mockResolvedValue(undefined);
  logout.mockResolvedValue(undefined);
  logoutAll.mockResolvedValue(undefined);
});

afterEach(() => cleanup());

describe('AccountMenu', () => {
  it('names the account button even where the visible name is hidden', () => {
    render(<AccountMenu />);
    // Below `sm` the name span is hidden and the initial is aria-hidden: only
    // the label names the button for a screen reader on a phone.
    expect(screen.getByRole('button', { name: 'Ada' })).toHaveAttribute('aria-label', 'Ada');
  });

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
  it('links to settings without duplicating the header dashboard entry', () => {
    render(<AccountMenu />);
    openMenu();
    expect(screen.queryByRole('menuitem', { name: 'Dashboard' })).not.toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: 'My settings' })).toHaveAttribute(
      'href',
      '/dashboard/settings',
    );
  });

  it('opens language options without changing preference and marks the current language', () => {
    render(<AccountMenu />);
    openMenu();
    fireEvent.pointerEnter(screen.getByRole('menuitem', { name: 'Language' }));
    expect(screen.getByRole('menuitemradio', { name: 'English' })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(screen.getByRole('menuitemradio', { name: '简体中文' })).toHaveAttribute(
      'aria-checked',
      'false',
    );
    expect(chooseLocale).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('menuitemradio', { name: '简体中文' }));
    expect(chooseLocale).toHaveBeenCalledWith('zh-CN');
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Ada' })).toHaveFocus();
  });

  it('leaves an already selected language alone', () => {
    render(<AccountMenu />);
    openMenu();
    fireEvent.click(screen.getByRole('menuitem', { name: 'Language' }));
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'English' }));
    expect(chooseLocale).not.toHaveBeenCalled();
  });

  it('offers explicit light, dark, and system appearance choices', () => {
    render(<AccountMenu />);
    openMenu();
    fireEvent.click(screen.getByRole('menuitem', { name: 'Appearance' }));
    expect(screen.getByRole('menuitemradio', { name: 'System' })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(screen.getByRole('menuitemradio', { name: 'Light' })).toHaveAttribute(
      'aria-checked',
      'false',
    );
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'System' }));
    expect(setTheme).not.toHaveBeenCalled();
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    openMenu();
    fireEvent.click(screen.getByRole('menuitem', { name: 'Appearance' }));
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Dark' }));
    expect(setTheme).toHaveBeenCalledTimes(1);
    expect(setTheme).toHaveBeenCalledWith('dark');
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('navigates menus by keyboard and restores focus one level at a time', () => {
    render(<AccountMenu />);
    const trigger = screen.getByRole('button', { name: 'Ada' });
    fireEvent.keyDown(trigger, { key: 'ArrowDown' });
    expect(screen.getByRole('menuitem', { name: 'My settings' })).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
    const appearance = screen.getByRole('menuitem', { name: 'Appearance' });
    expect(appearance).toHaveFocus();
    fireEvent.keyDown(appearance, { key: 'ArrowRight' });
    expect(screen.getByRole('menuitemradio', { name: 'Light' })).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: 'End' });
    expect(screen.getByRole('menuitemradio', { name: 'System' })).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: 'Home' });
    expect(screen.getByRole('menuitemradio', { name: 'Light' })).toHaveFocus();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(appearance).toHaveFocus();
    expect(screen.queryByRole('menuitemradio')).not.toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(trigger).toHaveFocus();
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });

  it('wraps keyboard navigation and opens at the end with ArrowUp', () => {
    render(<AccountMenu />);
    fireEvent.keyDown(screen.getByRole('button', { name: 'Ada' }), { key: 'ArrowUp' });
    expect(screen.getByRole('menuitem', { name: 'Log out of all accounts' })).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
    expect(screen.getByRole('menuitem', { name: 'My settings' })).toHaveFocus();
  });

  it('returns from a submenu with ArrowLeft and dismisses on outside pointer down', () => {
    render(<AccountMenu />);
    openMenu();
    const language = screen.getByRole('menuitem', { name: 'Language' });
    fireEvent.click(language);
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowLeft' });
    expect(language).toHaveFocus();
    expect(screen.queryByRole('menuitemradio')).not.toBeInTheDocument();
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });
  it('dismisses on Tab without trapping focus inside a removed submenu', () => {
    render(<AccountMenu />);
    openMenu();
    fireEvent.click(screen.getByRole('menuitem', { name: 'Appearance' }));
    fireEvent.keyDown(document.activeElement!, { key: 'Tab' });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Ada' })).toHaveFocus();
  });
});
