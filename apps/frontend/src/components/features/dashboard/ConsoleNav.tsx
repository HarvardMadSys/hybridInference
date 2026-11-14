'use client';

import { useEffect, useRef } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useAuth } from '@/components/providers';
import { hasRole } from '@/components/providers/AuthProvider';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/useT';
import { revealHorizontally } from '@/components/ui/revealHorizontally';
import { useBatchCapabilities } from '@/lib/hooks/queries/useBatchCapabilities';

/**
 * The Agents link, naming the account this console tab is using.
 *
 * The Agent signs in on its own and may be using a different account of this
 * browser; given this one, it can say so and offer to switch rather than
 * silently showing another account's work.
 */
function agentsEntryUrl(agentsUrl: string, userId: string | undefined): string {
  if (!userId) return agentsUrl;
  const separator = agentsUrl.includes('?') ? '&' : '?';
  return `${agentsUrl}${separator}gateway_account=${encodeURIComponent(userId)}`;
}

interface NavItem {
  href: string;
  label: string;
  /** Leaves the console: a plain anchor, and a new tab when `newTab`. */
  external?: boolean;
  newTab?: boolean;
}

/** Whether `href` is the page being shown; Home matches only itself. */
export function isCurrent(pathname: string, href: string): boolean {
  if (href === '/dashboard') return pathname === href;
  return pathname === href || pathname.startsWith(`${href}/`);
}

const ITEM =
  'flex min-h-9 items-center whitespace-nowrap rounded-md px-3 py-1.5 text-sm font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400 md:px-4';
const ITEM_CURRENT = 'bg-gray-900 text-white';
const ITEM_OTHER = 'text-gray-600 hover:bg-gray-100 hover:text-gray-900';

/**
 * Navigation for everyday use, followed by a separate administrator group.
 *
 * From `md` up, a rail down the left edge of the window that stays in view
 * while the page scrolls. Below that it is one row that scrolls sideways, as
 * the admin tabs do, so the page starts near the top of a phone screen.
 */
export function ConsoleNav() {
  const pathname = usePathname() ?? '';
  const { state } = useAuth();
  const { features, agentsUrl } = useSiteConfig();
  const t = useT();
  const { capabilities: batch, error: batchError } = useBatchCapabilities();
  const scrollerRef = useRef<HTMLElement>(null);

  // Each entry is offered on its own condition; Batch history never grants
  // access to Chat, Agents or administration.
  const isInternal = hasRole(state.user?.role, 'internal');
  const isAdmin = state.user?.role === 'admin';
  const showAgents = isInternal && features.agents;
  const showBatches = batch ? batch.show_entry : Boolean(batchError && isInternal);

  const pages: NavItem[] = [
    { href: '/dashboard', label: t('console.nav.home', 'Home') },
    ...(isInternal
      ? [{ href: '/dashboard/playground', label: t('console.nav.chat', 'Chat') }]
      : []),
    ...(showAgents
      ? [{ href: agentsEntryUrl(agentsUrl, state.user?.id), label: 'Agents', external: true }]
      : []),
    { href: '/dashboard/keys', label: t('console.nav.keys', 'API Keys') },
    { href: '/dashboard/projects', label: t('console.nav.projects', 'Projects') },
    { href: '/dashboard/requests', label: t('console.nav.requests', 'Requests') },
    { href: '/dashboard/models', label: t('console.nav.models', 'Models') },
    ...(showBatches
      ? [{ href: '/dashboard/batches', label: t('console.nav.batches', 'Batches') }]
      : []),
    { href: '/dashboard/settings', label: t('console.nav.settings', 'Settings') },
  ];
  const adminPages: NavItem[] = isAdmin
    ? [
        { href: '/dashboard/admin/users', label: t('console.nav.admin', 'Admin Console') },
        { href: '/pgadmin/', label: 'pgAdmin', external: true, newTab: true },
      ]
    : [];

  // On a phone the row is wider than the screen: bring the current page into
  // view on first load, after every navigation, and when an entry that loads
  // later (Batches, after its capabilities arrive) moves it along the row.
  const entries = [...pages, ...adminPages].map((item) => item.href).join(' ');
  useEffect(() => {
    const scroller = scrollerRef.current;
    const active = scroller?.querySelector<HTMLElement>('[aria-current="page"]');
    if (scroller && active) revealHorizontally(scroller, active);
  }, [pathname, entries]);

  function renderItem(item: NavItem) {
    if (item.external) {
      return (
        <li key={item.href}>
          <a
            href={item.href}
            className={`${ITEM} ${ITEM_OTHER}`}
            {...(item.newTab ? { target: '_blank', rel: 'noopener noreferrer' } : {})}
          >
            {item.label}
          </a>
        </li>
      );
    }
    const current = isCurrent(pathname, item.href);
    return (
      <li key={item.href}>
        {/* Fetched on demand: the Playground and the admin area are large,
            and every link is always in view. */}
        <Link
          href={item.href}
          prefetch={false}
          aria-current={current ? 'page' : undefined}
          className={`${ITEM} ${current ? ITEM_CURRENT : ITEM_OTHER}`}
        >
          {item.label}
        </Link>
      </li>
    );
  }

  return (
    <nav
      ref={scrollerRef}
      aria-label={t('console.nav.label', 'Console')}
      className="shrink-0 overflow-x-auto border-b border-gray-200 px-3 py-2 [scrollbar-width:thin] md:w-60 md:overflow-visible md:border-b-0 md:border-r md:p-0"
    >
      <div className="flex w-max items-center gap-1 md:sticky md:top-14 md:max-h-[calc(100dvh-3.5rem)] md:w-auto md:flex-col md:items-stretch md:gap-5 md:overflow-y-auto md:px-4 md:py-5">
        <ul className="flex items-center gap-1 md:flex-col md:items-stretch">
          {pages.map(renderItem)}
        </ul>
        {adminPages.length > 0 && (
          <div className="flex items-center gap-1 border-gray-200 md:flex-col md:items-stretch md:border-t md:pt-5">
            <span aria-hidden="true" className="mx-1 h-5 w-px bg-gray-200 md:hidden" />
            <h2 className="hidden px-4 pb-1.5 text-[11px] font-medium uppercase tracking-wider text-gray-400 md:block">
              {t('console.nav.admin_group', 'Admin')}
            </h2>
            <ul
              aria-label={t('console.nav.admin_group', 'Admin')}
              className="flex items-center gap-1 md:flex-col md:items-stretch"
            >
              {adminPages.map(renderItem)}
            </ul>
          </div>
        )}
      </div>
    </nav>
  );
}
