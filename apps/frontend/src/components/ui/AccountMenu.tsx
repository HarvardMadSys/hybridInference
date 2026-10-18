'use client';

import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import toast from 'react-hot-toast';
import { useAuth } from '@/components/providers';
import { useLocale } from '@/components/providers/LocaleProvider';
import { useChooseLocale } from '@/components/providers/localeChoice';
import { useTheme } from '@/components/providers/ThemeProvider';
import type { Locale } from '@/lib/i18n/locales';
import { useT } from '@/components/providers/useT';
import type { AccountSummary } from '@/lib/api/auth';
import { getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { userAccountLabel, userDisplayName } from '@/lib/utils/userLabel';
import { roleLabel } from '@/lib/utils/roleLabel';

function Initial({ name, avatarUrl }: { name: string; avatarUrl?: string | null }) {
  if (avatarUrl && avatarUrl.startsWith('data:image/')) {
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={avatarUrl}
        alt=""
        aria-hidden="true"
        className="h-7 w-7 shrink-0 rounded-full object-cover ring-1 ring-gray-900/10"
      />
    );
  }
  return (
    <span
      aria-hidden="true"
      className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-gray-900 text-xs font-semibold uppercase text-white"
    >
      {name.slice(0, 1) || '?'}
    </span>
  );
}

function MenuIcon({ kind }: { kind: 'settings' | 'appearance' | 'language' }) {
  const paths = {
    settings:
      'M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.32 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.32 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831 2.34 2.34 0 0 1 2.33-4.033 2.34 2.34 0 0 0 3.32-1.915M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z',
    appearance: 'M12 3a9 9 0 1 0 0 18V3zm0 0a9 9 0 0 1 0 18',
    language: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zm0 0c5 5 5 13 0 18-5-5-5-13 0-18zM3 12h18',
  };
  return (
    <svg
      aria-hidden="true"
      className="h-4 w-4 shrink-0 text-gray-500"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d={paths[kind]} />
    </svg>
  );
}

type Submenu = 'appearance' | 'language';

function menuItems(menu: HTMLElement | null): HTMLElement[] {
  if (!menu) return [];
  return Array.from(
    menu.querySelectorAll<HTMLElement>('[role="menuitem"], [role="menuitemradio"]'),
  ).filter((item) => item.closest('[role="menu"]') === menu && !item.hasAttribute('disabled'));
}

/** Where "Add another account" lands: the sign-in form, returning here afterwards. */
function addAccountHref(pathname: string): string {
  return `/login?add=1&next=${encodeURIComponent(pathname || '/dashboard')}`;
}

/**
 * The signed-in account, the browser's other accounts, and the ways out.
 *
 * Switching changes the browser's account, not just this tab's: every other
 * console tab is told to reload (see `accountSync`). An account whose session
 * has ended is listed too, and choosing it goes to the sign-in form.
 */
export function AccountMenu() {
  const { state, accounts, maxAccounts, switchAccount, logout, logoutAll } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const t = useT();
  const locale = useLocale();
  const chooseLocale = useChooseLocale();
  const { theme, setTheme } = useTheme();
  const menuId = useId();
  const [open, setOpen] = useState(false);
  const [submenu, setSubmenu] = useState<Submenu | null>(null);
  const rootMenuRef = useRef<HTMLDivElement>(null);
  const submenuRef = useRef<HTMLDivElement>(null);
  const appearanceRef = useRef<HTMLButtonElement>(null);
  const languageRef = useRef<HTMLButtonElement>(null);
  const focusSubmenu = useRef(false);
  const openAtEnd = useRef(false);
  const [busy, setBusy] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);

  const close = () => {
    setOpen(false);
    setSubmenu(null);
  };

  useEffect(() => {
    if (!open) return;
    const items = menuItems(rootMenuRef.current);
    (openAtEnd.current ? items.at(-1) : items[0])?.focus();
  }, [open]);

  useEffect(() => {
    if (submenu && focusSubmenu.current) {
      menuItems(submenuRef.current)[0]?.focus();
      focusSubmenu.current = false;
    }
  }, [submenu]);

  useEffect(() => {
    if (!open) return;
    const onPointer = (event: PointerEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) close();
    };
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        if (submenu) {
          setSubmenu(null);
          (submenu === 'appearance' ? appearanceRef : languageRef).current?.focus();
        } else {
          close();
          buttonRef.current?.focus();
        }
      }
    };
    document.addEventListener('pointerdown', onPointer);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('pointerdown', onPointer);
      document.removeEventListener('keydown', onKey);
    };
  }, [open, submenu]);

  const user = state.user;
  if (!user) return null;
  const others = accounts.filter((account) => account.id !== user.id);
  const canAdd = accounts.length < maxAccounts;

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    try {
      await action();
    } catch (err) {
      toast.error(getErrorMessage(err));
    } finally {
      setBusy(false);
      close();
    }
  };

  const choose = (account: AccountSummary) => {
    if (!account.signed_in) {
      const identifier = account.email || account.login_name || '';
      router.push(
        `${addAccountHref(pathname)}${identifier ? `&identifier=${encodeURIComponent(identifier)}` : ''}`,
      );
      close();
      return;
    }
    void run(async () => {
      await switchAccount(account.id);
      toast.success(
        fill(t('chrome.account_menu.switched', 'Switched to {name}'), {
          name: userDisplayName(account),
        }),
      );
    });
  };

  const signOut = () =>
    run(async () => {
      await logout();
      router.replace(others.length > 0 ? '/login' : '/');
    });

  const signOutAll = () =>
    run(async () => {
      await logoutAll();
      router.replace('/');
    });

  const itemClass =
    'flex w-full items-center gap-3 rounded-md px-3 py-2 text-left text-sm text-gray-700 outline-none hover:bg-gray-100 focus:bg-gray-100 disabled:opacity-50';

  const onMenuKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Tab') {
      buttonRef.current?.focus();
      close();
      return;
    }
    const menu = (event.target as HTMLElement).closest<HTMLElement>('[role="menu"]');
    const items = menuItems(menu ?? null);
    const currentIndex = items.indexOf(document.activeElement as HTMLElement);
    let next: HTMLElement | undefined;
    if (event.key === 'ArrowDown') next = items[(currentIndex + 1) % items.length];
    if (event.key === 'ArrowUp') next = items[(currentIndex - 1 + items.length) % items.length];
    if (event.key === 'Home') next = items[0];
    if (event.key === 'End') next = items.at(-1);
    if (next) {
      event.preventDefault();
      next.focus();
    }
    if (event.key === 'ArrowLeft' && menu === submenuRef.current && submenu) {
      event.preventDefault();
      setSubmenu(null);
      (submenu === 'appearance' ? appearanceRef : languageRef).current?.focus();
    }
  };

  const openSubmenu = (next: Submenu, focus = false) => {
    focusSubmenu.current = focus;
    if (submenu === next && focus) menuItems(submenuRef.current)[0]?.focus();
    setSubmenu(next);
  };

  const selectLocale = async (next: Locale) => {
    close();
    buttonRef.current?.focus();
    if (next !== locale) await chooseLocale(next);
  };

  const renderSubmenu = (kind: Submenu, label: string) => (
    <div className="relative" onPointerEnter={() => openSubmenu(kind)}>
      <button
        ref={kind === 'appearance' ? appearanceRef : languageRef}
        type="button"
        role="menuitem"
        tabIndex={-1}
        aria-haspopup="menu"
        aria-expanded={submenu === kind}
        aria-controls={`${menuId}-${kind}`}
        onClick={() => openSubmenu(kind, true)}
        onKeyDown={(event) => {
          if (event.key === 'ArrowRight') {
            event.preventDefault();
            openSubmenu(kind, true);
          }
        }}
        className={`${itemClass} ${submenu === kind ? 'bg-gray-100' : ''}`}
      >
        <MenuIcon kind={kind} />
        <span className="flex-1">{label}</span>
        <span aria-hidden="true" className="rotate-90 text-gray-400 sm:rotate-180">
          ›
        </span>
      </button>
      {submenu === kind && (
        <div className="pt-1 sm:absolute sm:right-full sm:top-0 sm:pr-2 sm:pt-0">
          <div
            ref={submenuRef}
            id={`${menuId}-${kind}`}
            role="menu"
            aria-label={label}
            className="w-full rounded-lg border border-gray-200 bg-white p-1 shadow-lg sm:w-48"
          >
            {kind === 'appearance'
              ? (
                  [
                    ['light', t('chrome.appearance.light', 'Light')],
                    ['dark', t('chrome.appearance.dark', 'Dark')],
                    ['system', t('chrome.appearance.system', 'System')],
                  ] as const
                ).map(([value, name]) => (
                  <button
                    key={value}
                    type="button"
                    role="menuitemradio"
                    tabIndex={-1}
                    aria-checked={theme === value}
                    onClick={() => {
                      if (value !== theme) setTheme(value);
                      close();
                      buttonRef.current?.focus();
                    }}
                    className={itemClass}
                  >
                    <span className="flex-1">{name}</span>
                    {theme === value && (
                      <span aria-hidden="true" className="text-crimson">
                        ✓
                      </span>
                    )}
                  </button>
                ))
              : (
                  [
                    ['en', 'English'],
                    ['zh-CN', '简体中文'],
                  ] as const
                ).map(([value, name]) => (
                  <button
                    key={value}
                    type="button"
                    role="menuitemradio"
                    tabIndex={-1}
                    lang={value}
                    aria-checked={locale === value}
                    onClick={() => void selectLocale(value)}
                    className={itemClass}
                  >
                    <span className="flex-1">{name}</span>
                    {locale === value && (
                      <span aria-hidden="true" className="text-crimson">
                        ✓
                      </span>
                    )}
                  </button>
                ))}
          </div>
        </div>
      )}
    </div>
  );

  return (
    <div ref={containerRef} className="relative">
      <button
        ref={buttonRef}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={menuId}
        // The visible name is hidden below `sm` and the initial is aria-hidden,
        // so on a phone the button would otherwise have no accessible name.
        aria-label={userDisplayName(user)}
        onClick={() => {
          openAtEnd.current = false;
          if (open) close();
          else setOpen(true);
        }}
        onKeyDown={(event) => {
          if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            openAtEnd.current = event.key === 'ArrowUp';
            if (!open) setOpen(true);
            else {
              const items = menuItems(rootMenuRef.current);
              (openAtEnd.current ? items.at(-1) : items[0])?.focus();
            }
          }
        }}
        className="flex items-center gap-2 rounded-md px-2 py-1.5 text-sm font-medium text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-900"
      >
        <Initial name={userDisplayName(user)} avatarUrl={user.avatar_url} />
        <span className="hidden max-w-[10rem] truncate sm:inline">{userDisplayName(user)}</span>
      </button>
      {open && (
        <div
          ref={rootMenuRef}
          onKeyDown={onMenuKey}
          onPointerMove={(event) => {
            const item = (event.target as HTMLElement).closest('[role="menuitem"]');
            if (
              item?.closest('[role="menu"]') === rootMenuRef.current &&
              !item.hasAttribute('aria-haspopup')
            )
              setSubmenu(null);
          }}
          onFocus={(event) => {
            const target = event.target as HTMLElement;
            if (
              target.closest('[role="menu"]') === rootMenuRef.current &&
              !target.hasAttribute('aria-haspopup')
            )
              setSubmenu(null);
          }}
          id={menuId}
          role="menu"
          aria-label={t('chrome.account_menu.label', 'Account')}
          className="absolute right-0 z-40 mt-2 max-h-[calc(100dvh-7rem)] w-72 max-w-[calc(100vw-2rem)] overflow-y-auto rounded-xl sm:max-h-none sm:overflow-visible bg-white p-2 shadow-lg ring-1 ring-gray-200"
        >
          <div className="flex items-center gap-3 px-3 py-2">
            <Initial name={userDisplayName(user)} avatarUrl={user.avatar_url} />
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold text-gray-900">
                {userDisplayName(user)}
              </p>
              <p className="truncate text-xs text-gray-500">
                {userAccountLabel(user)} · {roleLabel(user.role, t)}
              </p>
            </div>
          </div>

          <div
            className="mt-1 border-t border-gray-100 pt-1"
            onPointerEnter={() => setSubmenu(null)}
          >
            <Link
              href="/dashboard/settings"
              role="menuitem"
              tabIndex={-1}
              onClick={close}
              className={itemClass}
            >
              <MenuIcon kind="settings" />
              {t('chrome.account_menu.settings', 'My settings')}
            </Link>
          </div>
          <div className="mt-1 border-t border-gray-100 pt-1">
            {renderSubmenu('appearance', t('chrome.appearance.label', 'Appearance'))}
            {renderSubmenu('language', t('chrome.account_menu.language', 'Language'))}
          </div>

          {others.length > 0 && (
            <div className="mt-1 border-t border-gray-100 pt-1">
              {others.map((account) => (
                <button
                  tabIndex={-1}
                  key={account.id}
                  type="button"
                  role="menuitem"
                  disabled={busy}
                  onClick={() => choose(account)}
                  className={itemClass}
                >
                  <Initial name={userDisplayName(account)} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate">{userDisplayName(account)}</span>
                    <span className="block truncate text-xs text-gray-500">
                      {account.signed_in
                        ? userAccountLabel(account)
                        : t('chrome.account_menu.signed_out', 'Signed out · sign in again')}
                    </span>
                  </span>
                </button>
              ))}
            </div>
          )}

          <div className="mt-1 border-t border-gray-100 pt-1">
            {canAdd ? (
              <button
                type="button"
                role="menuitem"
                tabIndex={-1}
                disabled={busy}
                onClick={() => {
                  close();
                  router.push(addAccountHref(pathname));
                }}
                className={itemClass}
              >
                {t('chrome.account_menu.add_account', 'Add another account')}
              </button>
            ) : (
              <p className="px-3 py-2 text-xs text-gray-500">
                {fill(
                  t(
                    'chrome.account_menu.limit_reached',
                    'This browser holds {max} accounts, the most it can. Sign one out to add another.',
                  ),
                  { max: String(maxAccounts) },
                )}
              </p>
            )}
            <button
              type="button"
              role="menuitem"
              tabIndex={-1}
              disabled={busy}
              onClick={signOut}
              className={itemClass}
            >
              {t('chrome.account_menu.log_out', 'Log out')}
            </button>
            {others.length > 0 && (
              <button
                type="button"
                role="menuitem"
                tabIndex={-1}
                disabled={busy}
                onClick={signOutAll}
                className={itemClass}
              >
                {t('chrome.account_menu.log_out_all', 'Log out of all accounts')}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
