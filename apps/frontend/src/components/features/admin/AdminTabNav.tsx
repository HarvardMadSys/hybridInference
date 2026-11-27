'use client';

import { useRef } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useRevealCurrent } from '@/components/ui/useRevealCurrent';

const TABS = [
  { slug: 'users', label: 'Users' },
  { slug: 'requests', label: 'Recent Requests' },
  { slug: 'providers', label: 'Providers' },
  { slug: 'routing', label: 'Routing' },
  { slug: 'batches', label: 'Batches' },
  { slug: 'log', label: 'Log' },
  { slug: 'announcements', label: 'Announcements' },
  { slug: 'analytics', label: 'Analytics' },
  { slug: 'settings', label: 'Settings' },
] as const;

export function AdminTabNav() {
  const pathname = usePathname() ?? '';
  const scrollerRef = useRef<HTMLElement>(null);

  // On a phone the row is wider than the screen: bring the current tab into
  // view on first load and after every navigation.
  useRevealCurrent(scrollerRef, pathname);

  return (
    // One row that scrolls sideways on a narrow screen rather than wrapping
    // into a block of tabs; the padding keeps focus rings inside the scroller.
    <nav
      ref={scrollerRef}
      aria-label="Admin sections"
      className="relative -mx-1 mt-3 overflow-x-auto px-1 py-1 [scrollbar-width:thin]"
    >
      <div className="flex w-max items-center gap-1">
        {TABS.map((tab) => {
          const href = `/dashboard/admin/${tab.slug}`;
          const isActive = pathname === href || pathname.startsWith(`${href}/`);
          return (
            // Every tab is always in view; default prefetch would pull every
            // admin route's JS up front, so fetch each tab on demand instead.
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
