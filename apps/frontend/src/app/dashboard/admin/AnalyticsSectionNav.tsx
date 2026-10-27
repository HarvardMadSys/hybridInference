'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

const ANALYTICS_PATH = '/dashboard/admin/analytics';

/** The Analytics tab's pages. Each has its own address, so a link opens it. */
const SECTIONS = [
  { href: ANALYTICS_PATH, label: 'Overview' },
  { href: `${ANALYTICS_PATH}/token-usage`, label: 'Token usage' },
  { href: `${ANALYTICS_PATH}/geo`, label: 'Request origins' },
] as const;

/** The segmented control above every Analytics page. */
export function AnalyticsSectionNav() {
  const pathname = usePathname() ?? '';
  return (
    <nav
      aria-label="Analytics sections"
      className="mb-4 inline-flex overflow-hidden rounded-lg border border-gray-200 bg-white"
    >
      {SECTIONS.map(({ href, label }) => {
        // The overview owns only its own address; every other page also owns
        // the addresses below it.
        const isActive =
          pathname === href || (href !== ANALYTICS_PATH && pathname.startsWith(`${href}/`));
        return (
          // prefetch off: Request origins carries the d3/topojson bundle.
          <Link
            key={href}
            href={href}
            prefetch={false}
            aria-current={isActive ? 'page' : undefined}
            className={`px-3.5 py-1.5 text-[13px] font-medium transition ${
              isActive ? 'bg-gray-900 text-white' : 'text-gray-600 hover:bg-gray-50'
            }`}
          >
            {label}
          </Link>
        );
      })}
    </nav>
  );
}
