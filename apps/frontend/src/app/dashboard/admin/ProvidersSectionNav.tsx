'use client';

import { useRef } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';

import {
  PROVIDERS_PATH,
  PROVIDER_PERFORMANCE_PATH,
} from '@/components/features/admin/providerRoutes';
import { useRevealCurrent } from '@/components/ui/useRevealCurrent';

/**
 * The Providers tab's pages: Overview, where a provider is set up -- the
 * registry and whether routing may use each provider, the keys, and the
 * quotas -- and Performance, where it is watched. Each has its own address,
 * so a link opens it. The model catalog is on the Routing tab.
 */
const SECTIONS = [
  { href: PROVIDERS_PATH, label: 'Overview' },
  { href: PROVIDER_PERFORMANCE_PATH, label: 'Performance' },
] as const;

/** The segmented control above every Providers page. */
export function ProvidersSectionNav() {
  const pathname = usePathname() ?? '';
  const scrollerRef = useRef<HTMLElement>(null);

  // On a narrow screen the control scrolls sideways, and brings the current
  // section into view on load and after every navigation.
  useRevealCurrent(scrollerRef, pathname);

  return (
    // The padding keeps focus rings inside the scroller; the negative top
    // margin takes it back, so the control sits where the other tabs' does.
    <nav
      ref={scrollerRef}
      aria-label="Providers sections"
      className="-mx-1 -mt-1 mb-3 overflow-x-auto px-1 py-1 [scrollbar-width:thin]"
    >
      <div className="inline-flex w-max overflow-hidden rounded-lg border border-gray-200 bg-white">
        {SECTIONS.map(({ href, label }) => {
          // Overview owns only its own address; every other page also owns the
          // addresses below it.
          const isActive =
            pathname === href || (href !== PROVIDERS_PATH && pathname.startsWith(`${href}/`));
          return (
            <Link
              key={href}
              href={href}
              prefetch={false}
              aria-current={isActive ? 'page' : undefined}
              className={`whitespace-nowrap px-3.5 py-1.5 text-[13px] font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-gray-400 ${
                isActive ? 'bg-gray-900 text-white' : 'text-gray-600 hover:bg-gray-50'
              }`}
            >
              {label}
            </Link>
          );
        })}
      </div>
    </nav>
  );
}
