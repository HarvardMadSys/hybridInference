'use client';

import { useEffect, useRef } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useT } from '@/components/providers/useT';
import { revealHorizontally } from '@/components/ui/revealHorizontally';

export const ORGANIZATION_ROOT = '/dashboard/organization';

/** The address of one page of the organization console. */
export function organizationHref(slug: string): string {
  return `${ORGANIZATION_ROOT}/${slug}`;
}

/** The row of tabs under the organization's name: one page each, as the admin area has. */
export function OrganizationTabNav() {
  const pathname = usePathname() ?? '';
  const t = useT();
  const scrollerRef = useRef<HTMLElement>(null);
  const tabs = [
    { slug: 'overview', label: t('organization.tabs.overview', 'Overview') },
    { slug: 'members', label: t('organization.tabs.members', 'Members') },
    { slug: 'usage', label: t('organization.tabs.usage', 'Usage') },
    { slug: 'audit-log', label: t('organization.tabs.audit', 'Audit log') },
    { slug: 'settings', label: t('organization.tabs.settings', 'Settings') },
  ];

  // On a phone the row is wider than the screen: bring the current tab into
  // view on first load and after every navigation.
  useEffect(() => {
    const scroller = scrollerRef.current;
    const active = scroller?.querySelector<HTMLElement>('[aria-current="page"]');
    if (scroller && active) revealHorizontally(scroller, active);
  }, [pathname]);

  return (
    <nav
      ref={scrollerRef}
      aria-label={t('organization.tabs.label', 'Organization sections')}
      className="relative -mx-1 mt-3 overflow-x-auto px-1 py-1 [scrollbar-width:thin]"
    >
      <div className="flex w-max items-center gap-1">
        {tabs.map((tab) => {
          const href = organizationHref(tab.slug);
          const isActive = pathname === href || pathname.startsWith(`${href}/`);
          return (
            // Fetched on demand, as the admin tabs are: every tab is in view.
            <Link
              key={tab.slug}
              href={href}
              prefetch={false}
              aria-current={isActive ? 'page' : undefined}
              className={`whitespace-nowrap rounded-md px-3 py-1.5 text-[13px] font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400 ${
                isActive
                  ? 'bg-gray-900 text-white'
                  : 'text-gray-500 hover:bg-gray-100 hover:text-gray-900'
              }`}
            >
              {tab.label}
            </Link>
          );
        })}
      </div>
    </nav>
  );
}
