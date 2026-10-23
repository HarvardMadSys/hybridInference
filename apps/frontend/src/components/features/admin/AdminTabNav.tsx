'use client';

import { useEffect, useRef } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';

const TABS = [
  { slug: 'users', label: 'Users' },
  { slug: 'requests', label: 'Recent Requests' },
  { slug: 'providers', label: 'Providers' },
  { slug: 'routing', label: 'Routing' },
  { slug: 'token-usage', label: 'Token Usage' },
  { slug: 'log', label: 'Log' },
  { slug: 'announcements', label: 'Announcements' },
  { slug: 'analytics', label: 'Analytics' },
  { slug: 'configuration', label: 'Configuration' },
  { slug: 'settings', label: 'Settings' },
] as const;

/**
 * Scroll `scroller` sideways just enough to show `item` whole. Only the
 * row's own `scrollLeft` moves: `scrollIntoView` would also scroll the page.
 */
export function revealHorizontally(scroller: HTMLElement, item: HTMLElement) {
  const box = scroller.getBoundingClientRect();
  const rect = item.getBoundingClientRect();
  // Positions within the row's scrollable content.
  const left = rect.left - box.left + scroller.scrollLeft;
  const right = left + rect.width;
  if (left < scroller.scrollLeft) {
    scroller.scrollLeft = Math.max(0, left - 16);
  } else if (right > scroller.scrollLeft + scroller.clientWidth) {
    scroller.scrollLeft = right - scroller.clientWidth + 16;
  }
}

export function AdminTabNav() {
  const pathname = usePathname() ?? '';
  const scrollerRef = useRef<HTMLElement>(null);

  // On a phone the row is wider than the screen: bring the current tab into
  // view on first load and after every navigation.
  useEffect(() => {
    const scroller = scrollerRef.current;
    const active = scroller?.querySelector<HTMLElement>('[aria-current="page"]');
    if (scroller && active) revealHorizontally(scroller, active);
  }, [pathname]);

  return (
    // One row that scrolls sideways on a narrow screen rather than wrapping
    // into a block of tabs; the padding keeps focus rings inside the scroller.
    <nav
      ref={scrollerRef}
      aria-label="Admin sections"
      className="-mx-1 mt-3 overflow-x-auto px-1 py-1 [scrollbar-width:thin]"
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
