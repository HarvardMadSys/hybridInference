'use client';

import { useEffect, useId, useRef, useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useAuth } from '@/components/providers';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/useT';
import { fill } from '@/lib/utils/interpolate';
import { AccountMenu } from './AccountMenu';
import { useConsoleWidth } from '@/components/ui/consoleWidth';

interface HeaderLink {
  key: string;
  href: string;
  label: string;
  /** Another site: a plain anchor that opens in a new tab. */
  external: boolean;
  /** A console page fetched on demand; the Dashboard keeps Next's prefetch. */
  prefetch?: false;
}

const BAR_LINK =
  'rounded-md px-2 py-1.5 text-sm font-medium text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-900 sm:px-3';
const MENU_ROW =
  'flex min-h-11 items-center rounded-md px-3 text-base transition-colors hover:bg-gray-100 hover:text-gray-900';
const MENU_LINK = `${MENU_ROW} font-medium text-gray-700`;

/**
 * The console's top bar.
 *
 * From `md` up every link sits in the bar. Below that the bar keeps the name,
 * a Menu button and the account menu, and the links open beneath it as a
 * list: a phone has no room for them in a row, and wrapped they stacked the
 * bar three lines deep above every page.
 */
export function Header() {
  const consoleWidth = useConsoleWidth();
  const { state } = useAuth();
  const { branding, features } = useSiteConfig();
  const t = useT();
  const pathname = usePathname();
  const menuId = useId();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuButtonRef = useRef<HTMLButtonElement>(null);

  // Arriving on another page closes the list.
  useEffect(() => {
    setMenuOpen(false);
  }, [pathname]);

  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      setMenuOpen(false);
      menuButtonRef.current?.focus();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [menuOpen]);

  // The status page is linked from the footer only.
  const links: HeaderLink[] = [];
  if (features.publicStats) {
    links.push({
      key: 'stats',
      href: '/stats',
      label: t('chrome.header.stats', 'Stats'),
      external: false,
      prefetch: false,
    });
  }
  if (branding.docsUrl) {
    links.push({
      key: 'docs',
      href: branding.docsUrl,
      label: t('chrome.header.docs', 'Docs'),
      external: true,
    });
  }
  for (const link of branding.navLinks) {
    links.push({ key: `nav:${link.url}`, href: link.url, label: link.label, external: true });
  }
  if (state.isAuthenticated) {
    if (features.rag) {
      links.push({
        key: 'chat',
        href: '/chat',
        label: t('chrome.header.docs_assistant', 'Docs Assistant'),
        external: false,
        prefetch: false,
      });
    }
    links.push({
      key: 'dashboard',
      href: '/dashboard',
      label: t('chrome.header.dashboard', 'Dashboard'),
      external: false,
    });
  }

  const organization =
    branding.orgName && branding.orgUrl ? { name: branding.orgName, url: branding.orgUrl } : null;
  const hasMenu = links.length > 0 || organization !== null;
  // Following a link closes the list, even one to the page already shown.
  const closeMenu = () => setMenuOpen(false);

  const renderLink = (link: HeaderLink, className: string) =>
    link.external ? (
      <a
        key={link.key}
        href={link.href}
        className={className}
        target="_blank"
        rel="noopener noreferrer"
        onClick={closeMenu}
      >
        {link.label}
      </a>
    ) : (
      <Link
        key={link.key}
        href={link.href}
        prefetch={link.prefetch}
        className={className}
        onClick={closeMenu}
      >
        {link.label}
      </Link>
    );

  return (
    <header
      className={`mx-auto flex w-full ${consoleWidth} flex-wrap items-center justify-between gap-3 px-4 py-4 sm:px-6 sm:py-6`}
    >
      <div className="flex min-w-0 items-baseline gap-2">
        <Link
          href="/"
          prefetch={false}
          className="inline-flex min-w-0 items-center gap-2 text-xl font-bold tracking-tight"
        >
          {branding.logoUrl ? (
            // Runtime assets cannot use next/image because their origin is
            // deliberately unknown when the neutral image is built.
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={branding.logoUrl}
              alt={fill(t('chrome.header.logo_alt', '{app_name} logo'), {
                app_name: branding.appName,
              })}
              className="h-8 w-auto shrink-0 object-contain"
            />
          ) : null}
          {branding.appName}
        </Link>
        {organization && (
          // Below `sm` the organization is in the menu instead, so the name,
          // the Menu button and the account fit on one line.
          <a
            href={organization.url}
            className="hidden font-serif text-sm text-gray-500 hover:text-crimson sm:inline"
            target="_blank"
            rel="noopener noreferrer"
          >
            {organization.name}
          </a>
        )}
      </div>
      <div className="ml-auto flex flex-wrap items-center justify-end gap-1 sm:gap-2">
        {/* `contents` makes the links items of this row from `md` up, laid out
            as they always were; below `md` the menu offers them instead. */}
        <div className="hidden md:contents">{links.map((link) => renderLink(link, BAR_LINK))}</div>
        {hasMenu && (
          <button
            ref={menuButtonRef}
            type="button"
            aria-expanded={menuOpen}
            aria-controls={menuId}
            aria-label={t('chrome.header.menu', 'Menu')}
            onClick={() => setMenuOpen((open) => !open)}
            className="inline-flex h-10 w-10 items-center justify-center rounded-md text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400 md:hidden"
          >
            <svg
              aria-hidden="true"
              className="h-5 w-5"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={1.75}
              strokeLinecap="round"
            >
              <path d={menuOpen ? 'M6 6l12 12M18 6L6 18' : 'M4 7h16M4 12h16M4 17h16'} />
            </svg>
          </button>
        )}
        {/* Last in the row, so its menu, which opens leftwards from its right
            edge, has the whole width of a phone to open into. */}
        {state.isAuthenticated && <AccountMenu />}
      </div>
      {hasMenu && menuOpen && (
        <nav
          id={menuId}
          aria-label={t('chrome.header.menu', 'Menu')}
          className="basis-full border-t border-gray-200 pt-2 md:hidden"
        >
          <ul className="flex flex-col">
            {links.map((link) => (
              <li key={link.key}>{renderLink(link, MENU_LINK)}</li>
            ))}
            {organization && (
              <li className={links.length > 0 ? 'mt-1 border-t border-gray-200 pt-1' : undefined}>
                <a
                  href={organization.url}
                  className={`${MENU_ROW} font-serif text-gray-500`}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={closeMenu}
                >
                  {organization.name}
                </a>
              </li>
            )}
          </ul>
        </nav>
      )}
    </header>
  );
}
